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
STYLE_DIGITAL_PAINTING = 'digital_painting'
STYLE_SCENE_SWAP = 'scene_swap'
STYLE_FACE_SWAP = 'face_swap'

class StyleVO:
        
        name = ""
        thumbnail = ""
        image = ""
        workflow = ""
        style =""
        def __init__(self, name, thumbnail,image,workflow,style):
            self.name = name
            self.image = image
            self.thumbnail = thumbnail
            self.workflow = workflow
            self.style = style

class GroupStyleVO:
    name = None
    style = None
    items :list[StyleVO] = None
    def __init__(self, name,style):
        self.name = name
        self.style = style
        self.items = []    


class PromptVO:
    prompt_id = ""
    input_image = None
    output_image = None
    def __init__(self, prompt_id):
        self.prompt_id = prompt_id

class ServerExtension:
    prompt_list:list[PromptVO] = []
    group_style_list:list[GroupStyleVO] = []

    def __init__(self):
        ServerExtension.instance = self

    async def load_styles_json(self)->list[GroupStyleVO]:
        with open(os.path.join('input','styles', 'styles_config.json')) as f:
            style_list_json = json.load(f)
            print(style_list_json)
            group_vo_list = []
            for group_data in style_list_json:
                group_name = group_data["name"]
                group_style = group_data["style"]
                group_vo = GroupStyleVO(group_name,group_style)
                for style in group_data["items"]:
                    name = style["name"]
                    thumbnail = style["thumbnail"]
                    image = style["image"]
                    workflow = style["workflow"]
                    style_vo = StyleVO(name, thumbnail, image,workflow,style=group_style)
                    group_vo.items.append(style_vo)
                group_vo_list.append(group_vo)
            return group_vo_list

            

    async def thumbnails(self, request):
            self.group_style_list = await self.load_styles_json()
            image_data_list = []
            for group_style in self.group_style_list:
                group = {}
                group["name"] = group_style.name
                group["style"] = group_style.style
                items = []
                for style in group_style.items:
                        print(style.name)
                        #folder_paths.get_input_directory()
                        file_path =  os.path.join('input',style.thumbnail)
                        with open(file_path, 'rb') as image_file:
                            image_data = base64.b64encode(image_file.read()).decode('utf-8')
                            items.append({
                        'filename': style.name,
                        'data': image_data,
                        'style': style.style,
                    })
                group["items"] = items            
                image_data_list.append(group)
            return {'thumbnails': image_data_list}
            # return web.json_response({'thumbnails': image_data_list})

    def get_default_style(self):
        return self.group_style_list[0].items[0]

    async def post_digital_painting(self,request,prompt_server):
        self.group_style_list = await self.load_styles_json()
        prompt_id = str(uuid.uuid4())
        print("got digital painting")
        post = await request.post()
        client_id = post.get("client_id")
        ref_name = post.get("ref_name")
        style = post.get("style")
        styleVO:StyleVO = None
        
        for style in self.group_style_list:
            for item in style.items:
                if item.name == ref_name:
                    styleVO = item
                    break

        if styleVO is None:
            styleVO = self.get_default_style()
            print("no style is selected, using default style's refname :",styleVO.name)
            print("no style is selected, using default style's workflow :",styleVO.workflow)
        else:            
            print("selected workflow_api :",styleVO.workflow)
        img = {'image': post.get("image")}
        upload_resp = await self.image_upload(img)
        input_filepath = upload_resp['filepath']

        if upload_resp == 400:
            return web.json_response({"error":"Image Upload Failed"},status=400)
        
        image_name = upload_resp["name"]
        response = {}
        response["image"]=upload_resp

        if image_name is not None:
            
            # if ref_name == "":
                
            #     prompt = json.load(open(os.path.join('input', 'styles', workflow_api)))
            #     prompt["12"]["inputs"]["image"] = image_name
            # else:
            prompt = json.load(open(os.path.join('input',styleVO.workflow)))
            print("style name :",styleVO.style)
            if styleVO.style == STYLE_FACE_SWAP:
                print("inside face swap prompt update")
                prompt["2"]["inputs"]["image"] = styleVO.image #'styles/'+ styleVO.name
                prompt["3"]["inputs"]["image"] =  image_name
            elif styleVO.style == STYLE_DIGITAL_PAINTING:
                print('inside digiital painting prompt update')
                prompt["12"]["inputs"]["image"] = image_name #'styles/'+ styleVO.name
                prompt['30']['inputs']['image'] = image_name
                prompt["3"]["inputs"]["seed"] = random.randint(1, 1125899906842600)
            else:
                print('inside swap scene prompt update')
                prompt["12"]["inputs"]["image"] = styleVO.image #'styles/'+ styleVO.name
                prompt['30']['inputs']['image'] = image_name
                prompt["3"]["inputs"]["seed"] = random.randint(1, 1125899906842600)
            
            number = prompt_server.number
            prompt_server.number += 1
            #print('prompt ',prompt)
            valid = execution.validate_prompt(prompt)
            extra_data ={"client_id": client_id}
            if valid[0]:
                promptvo = PromptVO(prompt_id)
                promptvo.input_image = input_filepath
                self.prompt_list.append(promptvo)
                outputs_to_execute = valid[2]
                # IMPORTANT
                # prompt queue in prompt server is a queue of tuples
                prompt_server.prompt_queue.put((number, prompt_id, prompt, extra_data, outputs_to_execute))
                response["prompt_id"] = prompt_id
                response["number"] = number
                response["node_errors"] = valid[3]
                return web.json_response(response)
            else:
                print("invalid prompt:", valid[1])
                response_json = {"error": valid[1], "node_errors": valid[3]}
                response_status = 400
                return web.json_response(response_json, status=response_status)
        else:
            response_json = {"error": "no client_id", "node_errors": []}
            response_status = 400
            return web.json_response(response_json, status=response_status)
            return web.json_response({"error": "no client_id", "node_errors": []}, status=400)
    
    async def image_upload(self,img):
        image = img["image"]
        overwrite = "false" # img["overwrite"]

        # image_upload_type = img["type"]
        upload_dir = folder_paths.get_input_directory()
        image_upload_type = "input"
        # upload_dir, image_upload_type = self.get_dir_by_type(image_upload_type)

        if image and image.file:
            filename = image.filename
            if not filename:
                return 400

            # subfolder = img["subfolder"]
            # full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
            full_output_folder = os.path.join(upload_dir)
            filepath = os.path.abspath(os.path.join(full_output_folder, filename))

            if os.path.commonpath((upload_dir, filepath)) != upload_dir:
                return 400

            if not os.path.exists(full_output_folder):
                os.makedirs(full_output_folder)

            #  to avoid overwriting the fie
            split = os.path.splitext(filename)
            i = 1
            while os.path.exists(filepath):
                filename = f"{split[0]} ({i}){split[1]}"
                filepath = os.path.join(full_output_folder, filename)
                i += 1

            with open(filepath, "wb") as f:
                f.write(image.file.read())
            print('image uploaded at',filepath)
            return {"name" : filename, "type": image_upload_type,'filepath':filepath}
        else:
            return 400

    async def view_extention_image(self,request):
        print("view extension api called");
        prompt_id = request.rel_url.query["prompt_id"]
        for prompt in self.prompt_list:
            if prompt.prompt_id == prompt_id:
                os.remove(prompt.input_image)
                print('Removed input image',prompt.input_image)
                if os.path.isfile(prompt.output_image):
                    with open(prompt.output_image, 'rb') as image_file:
                        image_data = base64.b64encode(image_file.read()).decode('utf-8')
                        response_data = {
                            'filename': prompt.prompt_id,
                            'data': image_data
                        }
                    os.remove(prompt.output_image)
                    
                    print('Removed output image',prompt.output_image)
                    # end
                    self.prompt_list.remove(prompt)
                    print('Done : view extension image response filename and data')
                    return web.json_response(response_data)
                break
        return web.Response(status=404)
       

    def get_dir_by_type(self,dir_type):
        if dir_type is None:
            dir_type = "input"
        if dir_type == "input":
            type_dir = folder_paths.get_input_directory()
        elif dir_type == "temp":
            type_dir = folder_paths.get_temp_directory()
        elif dir_type == "output":
            type_dir = folder_paths.get_output_directory()

        return type_dir, dir_type

    

 