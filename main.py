from fastapi import FastAPI, WebSocket, BackgroundTasks, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
import yt_dlp
import json
import os
import asyncio
from typing import Dict
import re

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 挂载downloads目录为静态文件目录
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# 存储下载进度的字典
download_progress: Dict[str, dict] = {}

def sanitize_filename(filename: str) -> str:
    """清理文件名中的非法字符"""
    # 移除或替换非法字符
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # 确保文件名不超过255字符
    return filename[:255]

def get_video_info(url: str):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,  # 修改为False以获取完整信息
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"Error extracting video info: {e}")
        return None

async def download_video(url: str, video_id: str):
    def progress_hook(d):
        if d['status'] == 'downloading':
            try:
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                download_progress[video_id] = {
                    'status': 'downloading',
                    'progress': float(d.get('downloaded_bytes', 0)) / float(total_bytes or 1) * 100,
                    'speed': d.get('speed', 0),
                    'downloaded_bytes': d.get('downloaded_bytes', 0),
                    'total_bytes': total_bytes,
                    'eta': d.get('eta', 0),
                    'filename': d.get('filename', ''),
                    'error': None
                }
            except Exception as e:
                print(f"Error in progress_hook: {e}")

        elif d['status'] == 'finished':
            download_progress[video_id] = {
                'status': 'finished',
                'progress': 100,
                'speed': 0,
                'downloaded_bytes': d.get('total_bytes', 0),
                'total_bytes': d.get('total_bytes', 0),
                'eta': 0,
                'filename': d.get('filename', ''),
                'error': None
            }

    ydl_opts = {
        'format': 'best',  # 或者使用 'bestvideo+bestaudio/best'
        'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'restrictfilenames': True,  # 使用限制性文件名
        'noplaylist': True,  # 不下载播放列表
        'nocheckcertificate': True,  # 忽略证书验证
        'ignoreerrors': False,  # 不忽略错误
        'no_warnings': False,  # 显示警告
        'verbose': True,  # 显示详细信息
    }
    
    try:
        # 设置初始状态
        download_progress[video_id] = {
            'status': 'starting',
            'progress': 0,
            'speed': 0,
            'downloaded_bytes': 0,
            'total_bytes': 0,
            'eta': 0,
            'filename': '',
            'error': None
        }
        
        # 先获取视频信息
        video_info = await asyncio.to_thread(get_video_info, url)
        if not video_info:
            raise Exception("无法获取视频信息")

        # 修改输出模板，使用安全的文件名
        safe_title = sanitize_filename(video_info.get('title', 'video'))
        ydl_opts['outtmpl'] = str(DOWNLOAD_DIR / f'{safe_title}.%(ext)s')
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Starting download for video: {url}")
            info = await asyncio.to_thread(ydl.download, [url])
            print(f"Download completed with info: {info}")
            
            if info == 0:  # yt-dlp returns 0 on successful download
                save_video_info(video_info)
                return info
            else:
                raise Exception("下载失败")
                
    except Exception as e:
        error_msg = str(e)
        print(f"Download error: {error_msg}")
        download_progress[video_id] = {
            'status': 'error',
            'error': error_msg,
            'progress': 0
        }
        return None

def save_video_info(info: dict):
    if not info:
        return
        
    info_file = DOWNLOAD_DIR / "videos.json"
    videos = []
    if info_file.exists():
        try:
            with open(info_file, 'r', encoding='utf-8') as f:
                videos = json.load(f)
        except json.JSONDecodeError:
            videos = []
    
    # 使用安全的文件名
    safe_title = sanitize_filename(info.get('title', ''))
    video_data = {
        'title': info.get('title'),
        'duration': info.get('duration'),
        'uploader': info.get('uploader'),
        'description': info.get('description'),
        'filename': f"{safe_title}.{info.get('ext', 'mp4')}",
    }
    
    # 确保所有值都不是None
    video_data = {k: v if v is not None else '' for k, v in video_data.items()}
    
    videos.append(video_data)
    
    try:
        with open(info_file, 'w', encoding='utf-8') as f:
            json.dump(videos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving video info: {e}")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # 读取已下载视频列表
    videos = []
    info_file = DOWNLOAD_DIR / "videos.json"
    if info_file.exists():
        with open(info_file, 'r', encoding='utf-8') as f:
            videos = json.load(f)
    
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "videos": videos}
    )

@app.post("/download")
async def download(background_tasks: BackgroundTasks, url: str = Form()):
    video_id = url.split("v=")[-1]
    background_tasks.add_task(download_video, url, video_id)
    return {"message": "Download started", "video_id": video_id}

@app.websocket("/ws/{video_id}")
async def websocket_endpoint(websocket: WebSocket, video_id: str):
    await websocket.accept()
    try:
        while True:
            if video_id in download_progress:
                await websocket.send_json(download_progress[video_id])
                if download_progress[video_id]['status'] in ['finished', 'error']:
                    del download_progress[video_id]
                    break
            await asyncio.sleep(1)
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        await websocket.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8000) 