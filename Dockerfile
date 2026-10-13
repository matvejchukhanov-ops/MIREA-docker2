# Сервер файлового менеджера. Сборка в две стадии: в итоговый образ попадает
# только то, что нужно для работы, без промежуточных файлов сборки.

# --- Стадия 1: сборка ---
# Компиляция в байт-код заранее: синтаксическая ошибка остановит сборку образа,
# а не запуск контейнера, и интерпретатору не нужно писать .pyc при старте.
FROM python:3.12-slim AS builder
WORKDIR /src
COPY common ./common
COPY server ./server
RUN python -m compileall -q common server

# --- Стадия 2: итоговый образ ---
FROM python:3.12-slim

# Отдельный пользователь без прав root: если сервер взломают, атакующий
# окажется в контейнере без прав администратора.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=root:root /src /app

# Писать сервер может только в каталоги данных. Код принадлежит root и
# процессу приложения недоступен для изменения.
RUN mkdir -p data storage && chown app:app data storage

USER app

# Внутри контейнера сервер слушает все интерфейсы, иначе проброс порта не
# заработает. Наружу порт публикуется при запуске: -p 127.0.0.1:9000:9000.
ENV FM_HOST=0.0.0.0 \
    FM_PORT=9000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 9000
VOLUME ["/app/data", "/app/storage"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 9000), 2).close()"]

CMD ["python", "-m", "server.main"]
