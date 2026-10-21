"""Qwen-Image-2.1 工作台 —— 本地单进程服务端。

分层：

    server.py    Starlette 路由 + uvicorn 入口
    comfy.py     ←→ ComfyUI :8188（HTTP 同步 + WS 在 watcher 线程里）
    watcher.py   WS 读线程 + /history 轮询 → 状态与 SSE
    jobs.py      任务状态落盘（state/jobs/*.json + state/runs.jsonl）
    events.py    SSE 广播 + 断线重放
    static/      零构建前端

与 ComfyUI 的分工是刻意的：**队列用 ComfyUI 自己的**。`POST /prompt` 天然排队，
所以这里不维护 worker，只跟踪与落盘 —— 工作台重启后还能靠 `/queue` + `/history`
把在跑的任务认回来。
"""
