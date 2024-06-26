import os
import sys
import asyncio
import traceback
import base64
import nodes
import random
import folder_paths
import execution
import uuid
import urllib
import json
import glob
import struct
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo
from io import BytesIO
from server_extension import ServerExtension

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("Module 'aiohttp' not installed. Please install it via:")
    print("pip install aiohttp")
    print("or")
    print("pip install -r requirements.txt")
    sys.exit()

import mimetypes
from comfy.cli_args import args
import comfy.utils
import comfy.model_management

from app.user_manager import UserManager

class BinaryEventTypes:
    PREVIEW_IMAGE = 1
    UNENCODED_PREVIEW_IMAGE = 2

async def send_socket_catch_exception(function, message):
    try:
        await function(message)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, ConnectionResetError) as err:
        print("send error:", err)

@web.middleware
async def cache_control(request: web.Request, handler):
    response: web.Response = await handler(request)
    if request.path.endswith('.js') or request.path.endswith('.css'):
        response.headers.setdefault('Cache-Control', 'no-cache')
    return response

def create_cors_middleware(allowed_origin: str):
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            # Pre-flight request. Reply successfully:
            response = web.Response()
        else:
            response = await handler(request)

        response.headers['Access-Control-Allow-Origin'] = allowed_origin
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, DELETE, PUT, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    return cors_middleware

class PromptServer():
    

    def __init__(self, loop):
        PromptServer.instance = self
        
        mimetypes.init()
        mimetypes.types_map['.js'] = 'application/javascript; charset=utf-8'

        self.user_manager = UserManager()
        self.supports = ["custom_nodes_from_web"]
        self.prompt_queue = None
        self.loop = loop
        self.messages = asyncio.Queue()
        self.number = 0

        middlewares = [cache_control]
        if args.enable_cors_header:
            middlewares.append(create_cors_middleware(args.enable_cors_header))

        max_upload_size = round(args.max_upload_size * 1024 * 1024)
        self.app = web.Application(client_max_size=max_upload_size, middlewares=middlewares)
        self.sockets = dict()
        self.web_root = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), "web")
        routes = web.RouteTableDef()
        self.routes = routes
        self.last_node_id = None
        self.client_id = None
        self.progress = {"value": 0, "max": 20, "prompt_id": None, "node": self.last_node_id}

        self.on_prompt_handlers = []
        @routes.post("/digital-painting")
        async def post_digital_painting(request):
            server_extension = ServerExtension()
            return await server_extension.post_digital_painting(request, self)

        @routes.get("/thumbnails")
        async def thumbnails(request):
            server_extension = ServerExtension()
            thumbnails_json = await server_extension.thumbnails(request)
            return web.json_response(thumbnails_json)
            return await server_extension.thumbnails(request,self) 
        @routes.get('/ws')
        async def websocket_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sid = request.rel_url.query.get('clientId', '')
            if sid:
                # Reusing existing session, remove old
                self.sockets.pop(sid, None)
            else:
                sid = uuid.uuid4().hex

            self.sockets[sid] = ws

            try:
                # Send initial state to the new client
                await self.send("status", { "status": self.get_queue_info(), 'sid': sid }, sid)
                # On reconnect if we are the currently executing client send the current node
                if self.client_id == sid and self.last_node_id is not None:
                    await self.send("executing", { "node": self.last_node_id }, sid)
                    
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        print('ws connection closed with exception %s' % ws.exception())
            finally:
                self.sockets.pop(sid, None)
            return ws
        
        @routes.get("/prompt_status/{prompt_id}")
        async def get_prompt_status(request):
            prompt_id = request.match_info.get("prompt_id", None)
            if prompt_id == self.progress["prompt_id"]:
                progress = self.progress
                if "status" not in self.progress:
                    progress["status"]=200
                return web.json_response(self.progress)
            else:
                return web.json_response({"status":400,"error": "prompt_id not found"})
            
        @routes.get("/")
        async def get_root(request):
            return web.FileResponse(os.path.join(self.web_root, "index.html"))

        @routes.get("/embeddings")
        def get_embeddings(self):
            embeddings = folder_paths.get_filename_list("embeddings")
            return web.json_response(list(map(lambda a: os.path.splitext(a)[0], embeddings)))

        @routes.get("/extensions")
        async def get_extensions(request):
            files = glob.glob(os.path.join(
                glob.escape(self.web_root), 'extensions/**/*.js'), recursive=True)
            
            extensions = list(map(lambda f: "/" + os.path.relpath(f, self.web_root).replace("\\", "/"), files))
            
            for name, dir in nodes.EXTENSION_WEB_DIRS.items():
                files = glob.glob(os.path.join(glob.escape(dir), '**/*.js'), recursive=True)
                extensions.extend(list(map(lambda f: "/extensions/" + urllib.parse.quote(
                    name) + "/" + os.path.relpath(f, dir).replace("\\", "/"), files)))

            return web.json_response(extensions)

        def get_dir_by_type(dir_type):
            if dir_type is None:
                dir_type = "input"

            if dir_type == "input":
                type_dir = folder_paths.get_input_directory()
            elif dir_type == "temp":
                type_dir = folder_paths.get_temp_directory()
            elif dir_type == "output":
                type_dir = folder_paths.get_output_directory()

            return type_dir, dir_type

        def image_upload(post, image_save_function=None):
            image = post.get("image")
            overwrite = post.get("overwrite")

            image_upload_type = post.get("type")
            upload_dir, image_upload_type = get_dir_by_type(image_upload_type)

            if image and image.file:
                filename = image.filename
                if not filename:
                    return web.Response(status=400)

                subfolder = post.get("subfolder", "")
                full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
                filepath = os.path.abspath(os.path.join(full_output_folder, filename))

                if os.path.commonpath((upload_dir, filepath)) != upload_dir:
                    return web.Response(status=400)

                if not os.path.exists(full_output_folder):
                    os.makedirs(full_output_folder)

                split = os.path.splitext(filename)

                if overwrite is not None and (overwrite == "true" or overwrite == "1"):
                    pass
                else:
                    i = 1
                    while os.path.exists(filepath):
                        filename = f"{split[0]} ({i}){split[1]}"
                        filepath = os.path.join(full_output_folder, filename)
                        i += 1

                if image_save_function is not None:
                    image_save_function(image, post, filepath)
                else:
                    with open(filepath, "wb") as f:
                        f.write(image.file.read())

                return web.json_response({"name" : filename, "subfolder": subfolder, "type": image_upload_type})
            else:
                return web.Response(status=400)

        @routes.post("/upload/image")
        async def upload_image(request):
            post = await request.post()
            return image_upload(post)

        @routes.post("/remove")
        async def remove_image(request):
            post = await request.post()
            image_upload_type = post.get("type")
            upload_dir, image_upload_type = get_dir_by_type(image_upload_type)
            filename = post.get("name")
            subfolder = post.get("subfolder", "")
            full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
            filepath = os.path.abspath(os.path.join(full_output_folder, filename))
            if os.path.exists(filepath):
                os.remove(filepath)
                return web.Response(status=200)
            else:
                return web.Response(status=400)

       
        @routes.post("/upload/mask")
        async def upload_mask(request):
            post = await request.post()

            def image_save_function(image, post, filepath):
                original_ref = json.loads(post.get("original_ref"))
                filename, output_dir = folder_paths.annotated_filepath(original_ref['filename'])

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = original_ref.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if original_ref.get("subfolder", "") != "":
                    full_output_dir = os.path.join(output_dir, original_ref["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    with Image.open(file) as original_pil:
                        metadata = PngInfo()
                        if hasattr(original_pil,'text'):
                            for key in original_pil.text:
                                metadata.add_text(key, original_pil.text[key])
                        original_pil = original_pil.convert('RGBA')
                        mask_pil = Image.open(image.file).convert('RGBA')

                        # alpha copy
                        new_alpha = mask_pil.getchannel('A')
                        original_pil.putalpha(new_alpha)
                        original_pil.save(filepath, compress_level=4, pnginfo=metadata)

            return image_upload(post, image_save_function)

        @routes.get("/view")
        async def view_image(request):
            if "filename" in request.rel_url.query:
                filename = request.rel_url.query["filename"]
                filename,output_dir = folder_paths.annotated_filepath(filename)

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = request.rel_url.query.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if "subfolder" in request.rel_url.query:
                    full_output_dir = os.path.join(output_dir, request.rel_url.query["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                filename = os.path.basename(filename)
                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    if 'preview' in request.rel_url.query:
                        with Image.open(file) as img:
                            preview_info = request.rel_url.query['preview'].split(';')
                            image_format = preview_info[0]
                            if image_format not in ['webp', 'jpeg'] or 'a' in request.rel_url.query.get('channel', ''):
                                image_format = 'webp'

                            quality = 90
                            if preview_info[-1].isdigit():
                                quality = int(preview_info[-1])

                            buffer = BytesIO()
                            if image_format in ['jpeg'] or request.rel_url.query.get('channel', '') == 'rgb':
                                img = img.convert("RGB")
                            img.save(buffer, format=image_format, quality=quality)
                            buffer.seek(0)
                            return web.Response(body=buffer.read(), content_type=f'image/{image_format}',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    if 'channel' not in request.rel_url.query:
                        channel = 'rgba'
                    else:
                        channel = request.rel_url.query["channel"]

                    if channel == 'rgb':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                r, g, b, a = img.split()
                                new_img = Image.merge('RGB', (r, g, b))
                            else:
                                new_img = img.convert("RGB")

                            buffer = BytesIO()
                            new_img.save(buffer, format='PNG')
                            buffer.seek(0)
                            return web.Response(body=buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    elif channel == 'a':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                _, _, _, a = img.split()
                            else:
                                a = Image.new('L', img.size, 255)

                            # alpha img
                            alpha_img = Image.new('RGBA', img.size)
                            alpha_img.putalpha(a)
                            alpha_buffer = BytesIO()
                            alpha_img.save(alpha_buffer, format='PNG')
                            alpha_buffer.seek(0)
                            return web.Response(body=alpha_buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})
                    else:
                        return web.FileResponse(file, headers={"Content-Disposition": f"filename=\"{filename}\""})

            return web.Response(status=404)

        @routes.get("/view_extention")
        async def view_extention(request):
            serverextention = ServerExtension()
            return await serverextention.view_extention_image(request)
            
        @routes.get("/view_metadata/{folder_name}")
        async def view_metadata(request):
            folder_name = request.match_info.get("folder_name", None)
            if folder_name is None:
                return web.Response(status=404)
            if not "filename" in request.rel_url.query:
                return web.Response(status=404)

            filename = request.rel_url.query["filename"]
            if not filename.endswith(".safetensors"):
                return web.Response(status=404)

            safetensors_path = folder_paths.get_full_path(folder_name, filename)
            if safetensors_path is None:
                return web.Response(status=404)
            out = comfy.utils.safetensors_header(safetensors_path, max_size=1024*1024)
            if out is None:
                return web.Response(status=404)
            dt = json.loads(out)
            if not "__metadata__" in dt:
                return web.Response(status=404)
            return web.json_response(dt["__metadata__"])

        @routes.get("/system_stats")
        async def get_queue(request):
            device = comfy.model_management.get_torch_device()
            device_name = comfy.model_management.get_torch_device_name(device)
            vram_total, torch_vram_total = comfy.model_management.get_total_memory(device, torch_total_too=True)
            vram_free, torch_vram_free = comfy.model_management.get_free_memory(device, torch_free_too=True)
            system_stats = {
                "system": {
                    "os": os.name,
                    "python_version": sys.version,
                    "embedded_python": os.path.split(os.path.split(sys.executable)[0])[1] == "python_embeded"
                },
                "devices": [
                    {
                        "name": device_name,
                        "type": device.type,
                        "index": device.index,
                        "vram_total": vram_total,
                        "vram_free": vram_free,
                        "torch_vram_total": torch_vram_total,
                        "torch_vram_free": torch_vram_free,
                    }
                ]
            }
            return web.json_response(system_stats)

        @routes.get("/prompt")
        async def get_prompt(request):
            return web.json_response(self.get_queue_info())

        def node_info(node_class):
            obj_class = nodes.NODE_CLASS_MAPPINGS[node_class]
            info = {}
            info['input'] = obj_class.INPUT_TYPES()
            info['output'] = obj_class.RETURN_TYPES
            info['output_is_list'] = obj_class.OUTPUT_IS_LIST if hasattr(obj_class, 'OUTPUT_IS_LIST') else [False] * len(obj_class.RETURN_TYPES)
            info['output_name'] = obj_class.RETURN_NAMES if hasattr(obj_class, 'RETURN_NAMES') else info['output']
            info['name'] = node_class
            info['display_name'] = nodes.NODE_DISPLAY_NAME_MAPPINGS[node_class] if node_class in nodes.NODE_DISPLAY_NAME_MAPPINGS.keys() else node_class
            info['description'] = obj_class.DESCRIPTION if hasattr(obj_class,'DESCRIPTION') else ''
            info['category'] = 'sd'
            if hasattr(obj_class, 'OUTPUT_NODE') and obj_class.OUTPUT_NODE == True:
                info['output_node'] = True
            else:
                info['output_node'] = False

            if hasattr(obj_class, 'CATEGORY'):
                info['category'] = obj_class.CATEGORY
            return info

        @routes.get("/object_info")
        async def get_object_info(request):
            out = {}
            for x in nodes.NODE_CLASS_MAPPINGS:
                try:
                    out[x] = node_info(x)
                except Exception as e:
                    print(f"[ERROR] An error occurred while retrieving information for the '{x}' node.", file=sys.stderr)
                    traceback.print_exc()
            return web.json_response(out)

        @routes.get("/object_info/{node_class}")
        async def get_object_info_node(request):
            node_class = request.match_info.get("node_class", None)
            out = {}
            if (node_class is not None) and (node_class in nodes.NODE_CLASS_MAPPINGS):
                out[node_class] = node_info(node_class)
            return web.json_response(out)

        @routes.get("/history")
        async def get_history(request):
            max_items = request.rel_url.query.get("max_items", None)
            if max_items is not None:
                max_items = int(max_items)
            return web.json_response(self.prompt_queue.get_history(max_items=max_items))

        @routes.get("/history/{prompt_id}")
        async def get_history(request):
            prompt_id = request.match_info.get("prompt_id", None)
            return web.json_response(self.prompt_queue.get_history(prompt_id=prompt_id))

        @routes.get("/queue")
        async def get_queue(request):
            queue_info = {}
            current_queue = self.prompt_queue.get_current_queue()
            queue_info['queue_running'] = current_queue[0]
            queue_info['queue_pending'] = current_queue[1]
            return web.json_response(queue_info)
        
        @routes.post("/prompt")
        async def post_prompt(request):
            print("got prompt")
            resp_code = 200
            out_string = ""
            json_data =  await request.json()
            json_data = self.trigger_on_prompt(json_data)

            if "number" in json_data:
                number = float(json_data['number'])
            else:
                number = self.number
                if "front" in json_data:
                    if json_data['front']:
                        number = -number

                self.number += 1

            if "prompt" in json_data:
                prompt = json_data["prompt"]
                valid = execution.validate_prompt(prompt)
                extra_data = {}
                if "extra_data" in json_data:
                    extra_data = json_data["extra_data"]

                if "client_id" in json_data:
                    extra_data["client_id"] = json_data["client_id"]
                if valid[0]:
                    prompt_id = str(uuid.uuid4())
                    outputs_to_execute = valid[2]
                    self.prompt_queue.put((number, prompt_id, prompt, extra_data, outputs_to_execute))
                    response = {"prompt_id": prompt_id, "number": number, "node_errors": valid[3]}
                    print("prompt response:", response)
                    return web.json_response(response)
                else:
                    print("invalid prompt:", valid[1])
                    return web.json_response({"error": valid[1], "node_errors": valid[3]}, status=400)
            else:
                return web.json_response({"error": "no prompt", "node_errors": []}, status=400)

        @routes.post("/queue")
        async def post_queue(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_queue()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    delete_func = lambda a: a[1] == id_to_delete
                    self.prompt_queue.delete_queue_item(delete_func)

            return web.Response(status=200)

        @routes.post("/interrupt")
        async def post_interrupt(request):
            nodes.interrupt_processing()
            return web.Response(status=200)

        @routes.post("/free")
        async def post_free(request):
            json_data = await request.json()
            unload_models = json_data.get("unload_models", False)
            free_memory = json_data.get("free_memory", False)
            if unload_models:
                self.prompt_queue.set_flag("unload_models", unload_models)
            if free_memory:
                self.prompt_queue.set_flag("free_memory", free_memory)
            return web.Response(status=200)

        @routes.post("/history")
        async def post_history(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_history()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    self.prompt_queue.delete_history_item(id_to_delete)

            return web.Response(status=200)   
        
    def add_routes(self):
        self.user_manager.add_routes(self.routes)
        self.app.add_routes(self.routes)

        for name, dir in nodes.EXTENSION_WEB_DIRS.items():
            self.app.add_routes([
                web.static('/extensions/' + urllib.parse.quote(name), dir, follow_symlinks=True),
            ])

        self.app.add_routes([
            web.static('/', self.web_root, follow_symlinks=True),
        ])

    def get_queue_info(self):
        prompt_info = {}
        exec_info = {}
        exec_info['queue_remaining'] = self.prompt_queue.get_tasks_remaining()
        prompt_info['exec_info'] = exec_info
        return prompt_info

    async def send(self, event, data, sid=None):
        if event == BinaryEventTypes.UNENCODED_PREVIEW_IMAGE:
            await self.send_image(data, sid=sid)
        elif isinstance(data, (bytes, bytearray)):
            await self.send_bytes(event, data, sid)
        else:
            await self.send_json(event, data, sid)

    def encode_bytes(self, event, data):
        if not isinstance(event, int):
            raise RuntimeError(f"Binary event types must be integers, got {event}")

        packed = struct.pack(">I", event)
        message = bytearray(packed)
        message.extend(data)
        return message

    async def send_image(self, image_data, sid=None):
        image_type = image_data[0]
        image = image_data[1]
        max_size = image_data[2]
        if max_size is not None:
            if hasattr(Image, 'Resampling'):
                resampling = Image.Resampling.BILINEAR
            else:
                resampling = Image.ANTIALIAS

            image = ImageOps.contain(image, (max_size, max_size), resampling)
        type_num = 1
        if image_type == "JPEG":
            type_num = 1
        elif image_type == "PNG":
            type_num = 2

        bytesIO = BytesIO()
        header = struct.pack(">I", type_num)
        bytesIO.write(header)
        image.save(bytesIO, format=image_type, quality=95, compress_level=1)
        preview_bytes = bytesIO.getvalue()
        await self.send_bytes(BinaryEventTypes.PREVIEW_IMAGE, preview_bytes, sid=sid)

    async def send_bytes(self, event, data, sid=None):
        message = self.encode_bytes(event, data)

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_bytes, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_bytes, message)

    async def send_json(self, event, data, sid=None):
        message = {"type": event, "data": data}

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_json, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_json, message)

    def send_sync(self, event, data, sid=None):
        self.loop.call_soon_threadsafe(
            self.messages.put_nowait, (event, data, sid))

    def queue_updated(self):
        self.send_sync("status", { "status": self.get_queue_info() })

    async def publish_loop(self):
        while True:
            msg = await self.messages.get()
            await self.send(*msg)

    async def start(self, address, port, verbose=True, call_on_start=None):
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, address, port)
        await site.start()

        if verbose:
            print("Starting server\n")
            print("To see the GUI go to: http://{}:{}".format(address, port))
        if call_on_start is not None:
            call_on_start(address, port)

    def add_on_prompt_handler(self, handler):
        self.on_prompt_handlers.append(handler)

    def trigger_on_prompt(self, json_data):
        for handler in self.on_prompt_handlers:
            try:
                json_data = handler(json_data)
            except Exception as e:
                print(f"[ERROR] An error occurred during the on_prompt_handler processing")
                traceback.print_exc()

        return json_data

    def task_done_update_server_extension(self,prompt_id,outputs,status):
        # this prompt 1 is prompt id
            # outputs  filename is image file name
            #             { 
            #     "29": {"images": [{"filename": "digi_paint_00003_.png", "subfolder": "", "type": "output"}]},
            #     "21": {"images": [{"filename": "digi_paint_00003_.png", "subfolder": "", "type": "output"}]}
            # } 
        file_name = ""    
        for key,value in outputs.items():
            for image in value["images"]:
                    file_name = image["filename"]
                    print("filename",file_name)
                    break
                    
        full_file_path = os.path.join(folder_paths.get_output_directory(), file_name)
        print("full path", full_file_path)
            
                        
        server_extension = ServerExtension()
        for prompt in server_extension.prompt_list:
            if prompt.prompt_id == prompt_id:
                prompt.output_image = full_file_path
                break 

# class StyleVO:
        
#         name = ""
#         thumbnail = ""
#         image = ""
#         workflow = ""
#         def __init__(self, name, thumbnail,image,workflow):
#             self.name = name
#             self.image = image
#             self.thumbnail = thumbnail
#             self.workflow = workflow

# class GroupStyleVO:
#     name = None
#     style = None
#     items :list[StyleVO] = None
#     def __init__(self, name):
#         self.name = name
#         self.items = []    


# class PromptVO:
#     prompt_id = ""
#     input_image = None
#     output_image = None
#     def __init__(self, prompt_id):
#         self.prompt_id = prompt_id

# class ServerExtension:
#     prompt_list:list[PromptVO] = []
#     group_style_list:list[GroupStyleVO] = []

#     def __init__(self):
#         ServerExtension.instance = self

#     async def load_styles_json(self)->list[GroupStyleVO]:
#         with open(os.path.join('input','styles', 'styles_config.json')) as f:
#             style_list_json = json.load(f)
#             group_vo_list = []
#             for group_data in style_list_json:
#                 group_name = group_data["name"]
#                 group_vo = GroupStyleVO(group_name)
#                 for style in group_data["items"]:
#                     name = style["name"]
#                     thumbnail = style["thumbnail"]
#                     image = style["image"]
#                     workflow = style["workflow"]
#                     style_vo = StyleVO(name, thumbnail, image,workflow)
#                     group_vo.items.append(style_vo)
#                 group_vo_list.append(group_vo)
#             return group_vo_list

            

#     async def thumbnails(self, request,prompt_server:PromptServer):
#             self.group_style_list = await self.load_styles_json()
#             image_data_list = []
#             for group_style in self.group_style_list:
#                 group = {}
#                 group["name"] = group_style.name
#                 items = []
#                 for style in group_style.items:
#                         print(style.name)
#                         file_path = os.path.join(style.thumbnail)
#                         with open(file_path, 'rb') as image_file:
#                             image_data = base64.b64encode(image_file.read()).decode('utf-8')
#                             items.append({
#                         'filename': style.name,
#                         'data': image_data
#                     })
#                 group["items"] = items            
#                 image_data_list.append(group)
#             return web.json_response({'thumbnails': image_data_list})

#     def get_default_style(self):
#         return self.group_style_list[0].items[0]

#     async def post_digital_painting(self,request,prompt_server:PromptServer):
#         self.group_style_list = await self.load_styles_json()
#         prompt_id = str(uuid.uuid4())
#         print("got digital painting")
#         post = await request.post()
#         client_id = post.get("client_id")
#         # user_prompt = post.get("user_prompt")
#         ref_name = post.get("ref_name")
#         # workflow_api = self.get_default_style().workflow #'workflow_api.json'
#         workflow_api = None
#         for style in self.group_style_list:
#             for item in style.items:
#                 if item.name == ref_name:
#                     workflow_api = item.workflow
#                     break
#         if workflow_api is None:
#             workflow_api = self.get_default_style().workflow
#             ref_name = self.get_default_style().name
            
#             print("no style is selected, using default style's refname :",ref_name)
#             print("no style is selected, using default style's workflow :",workflow_api)
#         else:            
#             print("selected workflow_api :",workflow_api)
#         # img = {'image': post.get("image"), 'overwrite': post.get("overwrite"), 'type': post.get("type"), 'subfolder': post.get("subfolder")}
#         img = {'image': post.get("image")}
#         upload_resp = await self.image_upload(img)
#         input_filepath = upload_resp['filepath']

#         if upload_resp == 400:
#             return web.json_response({"error":"Image Upload Failed"},status=400)
        
#         image_name = upload_resp["name"]
#         response = {}
#         response["image"]=upload_resp

#         if image_name is not None:
#             if ref_name == "":
#                 # prompt = json.load(open(os.path.join('input', 'styles', 'workflow_api.json')))
#                 prompt = json.load(open(os.path.join('input', 'styles', workflow_api)))
#                 prompt["12"]["inputs"]["image"] = image_name
#             else:
#                 # prompt = json.load(open(os.path.join('input', 'styles',ref_name.split('.')[0]+'.json')))
#                 prompt = json.load(open(os.path.join('input', 'styles',workflow_api)))
#                 prompt["12"]["inputs"]["image"] = 'styles/'+ ref_name
#                 prompt['30']['inputs']['image'] = image_name
#             prompt["3"]["inputs"]["seed"] = random.randint(1, 1125899906842600)
#             # if user_prompt != "":
#             #     prompt["6"]["inputs"]["text"] = user_prompt

#             number = prompt_server.number
#             prompt_server.number += 1
            
#             valid = execution.validate_prompt(prompt)
#             extra_data ={"client_id": client_id}
#             if valid[0]:
#                 promptvo = PromptVO(prompt_id)
#                 promptvo.input_image = input_filepath
#                 self.prompt_list.append(promptvo)
#                 outputs_to_execute = valid[2]
#                 prompt_server.prompt_queue.put((number, prompt_id, prompt, extra_data, outputs_to_execute))
#                 response["prompt_id"] = prompt_id
#                 response["number"] = number
#                 response["node_errors"] = valid[3]
#                 return web.json_response(response)
#             else:
#                 print("invalid prompt:", valid[1])
#                 return web.json_response({"error": valid[1], "node_errors": valid[3]}, status=400)
#         else:
#             return web.json_response({"error": "no client_id", "node_errors": []}, status=400)
    
#     async def image_upload(self,img, image_save_function=None):
#         image = img["image"]
#         overwrite = "false" # img["overwrite"]

#         # image_upload_type = img["type"]
#         upload_dir = folder_paths.get_input_directory()
#         image_upload_type = "input"
#         # upload_dir, image_upload_type = self.get_dir_by_type(image_upload_type)

#         if image and image.file:
#             filename = image.filename
#             if not filename:
#                 return 400

#             # subfolder = img["subfolder"]
#             # full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
#             full_output_folder = os.path.join(upload_dir)
#             filepath = os.path.abspath(os.path.join(full_output_folder, filename))

#             if os.path.commonpath((upload_dir, filepath)) != upload_dir:
#                 return 400

#             if not os.path.exists(full_output_folder):
#                 os.makedirs(full_output_folder)

#             #  to avoid overwriting the fie
#             split = os.path.splitext(filename)
#             i = 1
#             while os.path.exists(filepath):
#                 filename = f"{split[0]} ({i}){split[1]}"
#                 filepath = os.path.join(full_output_folder, filename)
#                 i += 1

#             with open(filepath, "wb") as f:
#                 f.write(image.file.read())
#             print('image uploaded at',filepath)
#             return {"name" : filename, "type": image_upload_type,'filepath':filepath}
#         else:
#             return 400

#     async def view_extention_image(self,request):
#         print("view extension api called");
#         prompt_id = request.rel_url.query["prompt_id"]
#         for prompt in self.prompt_list:
#             if prompt.prompt_id == prompt_id:
#                 os.remove(prompt.input_image)
#                 print('Removed input image',prompt.input_image)
#                 if os.path.isfile(prompt.output_image):
#                     with open(prompt.output_image, 'rb') as image_file:
#                         image_data = base64.b64encode(image_file.read()).decode('utf-8')
#                         response_data = {
#                             'filename': prompt.prompt_id,
#                             'data': image_data
#                         }
#                     os.remove(prompt.output_image)
                    
#                     print('Removed output image',prompt.output_image)
#                     # end
#                     self.prompt_list.remove(prompt)
#                     print('Done : view extension image response filename and data')
#                     return web.json_response(response_data)
#                 break
#         return web.Response(status=404)
       

#     def get_dir_by_type(self,dir_type):
#         if dir_type is None:
#             dir_type = "input"
#         if dir_type == "input":
#             type_dir = folder_paths.get_input_directory()
#         elif dir_type == "temp":
#             type_dir = folder_paths.get_temp_directory()
#         elif dir_type == "output":
#             type_dir = folder_paths.get_output_directory()

#         return type_dir, dir_type

    

 