# 极简镜像：只复制运行所需文件（.env 等本地密钥文件绝不进镜像）
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.py utils.py clip_store.py sinks.py router.py gemini_processor.py notion_writer.py category_config.py telegram_media.py telegram_source.py video_pipeline.py album_aggregator.py link_checker.py markdown_mirror.py mcp_server.py browser_clipper.py app.py rules.yaml ./
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers
