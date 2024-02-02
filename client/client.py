import websocket 
import uuid
import io
import gradio as gr
import numpy as np
from PIL import Image
import random
import json
import requests
import urllib.parse
class Image_Data():
    name: str
    subfolder: str
    type: str

class Client():
    only_digital_painting = False
    client_id = str(uuid.uuid4())
    server_address = None
    queue_prompt_url = None
    queue_prompt_digital_url = None
    get_image_url = None
    get_history_url = None
    upload_image_url = None
    status_websocket_url = None
    remove_image_url = None
    response_json = None

    input_image:Image_Data = None
    output_image:Image_Data = None

    def __init__(self,only_digital_painting):
        self.only_digital_painting = only_digital_painting  

    def queue_prompt(self,prompt):
        p = {"prompt": prompt, "client_id": self.client_id}
        data = json.dumps(p).encode('utf-8')
        req =  requests.post(self.queue_prompt_url, data=data)
        return req.json()
    
    def queue_prompt_digital(self,user_prompt,image_path):
        data = {
            "client_id": self.client_id,
            "user_prompt": user_prompt,
            "overwrite":None,
            "subfolder":"",
            "type":None
            }
        files = {"image":open(image_path, 'rb')}
        req =  requests.post(self.queue_prompt_digital_url, data=data,files=files)
        if req.status_code != 200:
            print("Error in digital painting prompt queueing:", req.text)
        else:
            img_json = req.json()["image"]  
            self.input_image = Image_Data()
            self.input_image.name = img_json['name']
            self.input_image.subfolder = img_json['subfolder']
            self.input_image.type = img_json['type']
            print("Image uploaded successfully!")
            response = req.json()
            del response['image']
            return response

    def get_image(self,img:Image_Data):
        data = {"filename": img.name, "subfolder": img.subfolder, "type": img.type}
        url_values = urllib.parse.urlencode(data)
        with requests.get(self.get_image_url+"?{}".format(url_values)) as response:
            return response.content

    def get_images(self,prompt_id)->list[Image.Image]:
        response = requests.get(self.get_history_url+"/{}".format(prompt_id))
        history = response.json()[prompt_id]
        img_arr = []
        for node_id in history['outputs']:
            node_output = history['outputs'][node_id]
            if 'images' in node_output:
                image = node_output['images'][-1]
                self.output_image = Image_Data()
                self.output_image.name = image['filename']
                self.output_image.subfolder = image['subfolder']
                self.output_image.type = image['type']

                image_data = self.get_image(self.output_image)
                im = Image.open(io.BytesIO(image_data))
                img_arr.append(im)
        print(len(img_arr))
        return img_arr
    
    def remove_image(self,img:Image_Data):
        response_json = {"name": img.name, "subfolder": img.subfolder, "type": img.type}
        response = requests.post(self.remove_image_url, data=response_json)
        if response.status_code == 200:
            print("Image removed successfully!")
        else:
            print("Image removal failed")
    
    def get_status_websocket(self,prompt_id,pr):
        ws = websocket.WebSocket()
        ws.connect(self.status_websocket_url)
        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                print(message)
                if message['type'] == 'progress':
                    data = message['data']
                    pr((data['value'],data['max']),desc=message['type'])
                else:
                    pr(progress=1,desc=message['type'])
                    if message['type'] == 'executing':
                        data = message['data']
                        if data['node'] is None and data['prompt_id'] == prompt_id:
                            break 
            else:
                continue
    
    def get_status_REST(self,prompt_id,pr):
        pass

    def upload_image(self,image_path):
        files = {"image":open(image_path, 'rb')}
        data ={
                "overwrite":None,
                "subfolder":"",
                "type":None
            }
        response =  requests.post(self.upload_image_url, files=files, data=data)
        if response.status_code == 200:
                response_json = response.json()  
                self.input_image = Image_Data()
                self.input_image.name = response_json['name']
                self.input_image.subfolder = response_json['subfolder']
                self.input_image.type = response_json['type']

                print("Image uploaded successfully!")
                return response_json['name']
        else:
            print("Image upload failed:", response.text)
            return None
    
    def load_workflow(self,user_prompt,name):
        prompt = json.load(open('workflow_api.json'))
        prompt["3"]["inputs"]["seed"] = random.randint(1, 1125899906842600)
        prompt["12"]["inputs"]["image"] = name
        if user_prompt != "":
            prompt["6"]["inputs"]["text"] = user_prompt
        return prompt
    
    def update_server_url(self,server_url):
        self.server_address = server_url
        self.queue_prompt_url = "{}/prompt".format(self.server_address)
        self.queue_prompt_digital_url = "{}/digital-painting".format(self.server_address)
        self.get_image_url = "{}/view".format(self.server_address)
        self.get_history_url = "{}/history".format(self.server_address)
        self.upload_image_url = "{}/upload/image".format(self.server_address)
        self.remove_image_url = "{}/remove".format(self.server_address)
        self.status_websocket_url = "ws://{}/ws?clientId={}".format(self.server_address[7:], self.client_id)

    def image_mod(self,image_path,user_prompt,status_method='websocket',pr=gr.Progress()):
        #image_name = self.upload_image(image_path)
        #prompt = self.load_workflow(user_prompt,image_name)
        response = self.queue_prompt_digital(user_prompt,image_path)
        print(response)
        prompt_id = response['prompt_id']
        if status_method == "websocket":
            self.get_status_websocket(prompt_id,pr)
        else :
            self.get_status_REST(prompt_id,pr)
        
        img_list = self.get_images(prompt_id)
        self.remove_image(self.output_image)
        self.remove_image(self.input_image)
        return img_list if not self.only_digital_painting else img_list[-1]
    

class Client_UI():
    only_digital_painting = False
    server_textbox = None
    input_image = None
    user_prompt = None
    submit_button = None
    output_painting = None
    iface = None

    def __init__(self,client:Client):
        self.only_digital_painting = client.only_digital_painting  
        with gr.Blocks() as self.iface:
            gr.Markdown("Image Processor")
            with gr.Row():
                with gr.Column():
                    self.server_textbox = gr.Textbox(label='Server Address',interactive=True)
                    self.input_image = gr.Image(type='filepath')
                    with gr.Accordion(label="Advanced",open=False):
                        self.user_prompt = gr.Textbox(label='Prompt')
                    self.submit_button = gr.Button(value="Submit")
                with gr.Column():
                    self.output_painting = gr.Image() if self.only_digital_painting else gr.Gallery()
            self.submit_button.click(client.image_mod,inputs=[self.input_image,self.user_prompt],outputs=self.output_painting)
            self.server_textbox.input(client.update_server_url,inputs=[self.server_textbox])

    def launch_gradio(self,server_url):
        self.server_textbox.value = server_url
        client.update_server_url(server_url)
        self.iface.queue().launch(share=True)

if __name__ == "__main__":
    server_url = "mesa-gardening-determining-ranking.trycloudflare.com"
    client = Client(only_digital_painting=True)
    client_ui = Client_UI(client)
    client_ui.launch_gradio(server_url)