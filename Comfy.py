import subprocess
import threading
import os
import time
import socket

class ComfyUIClient:
    def __init__(self, port=8188):
        self.port = port

    def install_dependencies(self):
        #subprocess.run(["wget", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"])
        subprocess.run(["dpkg", "-i", "cloudflared-linux-amd64.deb"])
        #ubprocess.run(["pip", "install", "xformers!=0.0.18", "insightface", "onnxruntime", "gradio", "-r", "requirements.txt", "--extra-index-url", "https://download.pytorch.org/whl/cu121"])

    def iframe_thread(self):
        while True:
            time.sleep(1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', self.port))
            if result == 0:
                break
            sock.close()
        print("\nComfyUI finished loading, trying to launch cloudflared (if it gets stuck here cloudflared is having issues)\n")

        p = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.port}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in p.stderr:
            l = line.decode()
            if "trycloudflare.com " in l:
                print("This is the URL to access ComfyUI:", l[l.find("http"):], end='')

    def run_main_script(self):
        threading.Thread(target=self.iframe_thread, daemon=True).start()
        subprocess.run(["python", "main.py", "--dont-print-server"])

if __name__ == "__main__":
    comfy_ui_client = ComfyUIClient()
    comfy_ui_client.install_dependencies()
    comfy_ui_client.run_main_script()