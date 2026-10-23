# Образы файлового менеджера: TCP-сервер и веб-интерфейс. Сборка в несколько
# стадий: в итоговые образы попадает только то, что нужно для работы.
#
#   docker build -t filemanager .                    сервер (последняя стадия)
#   docker build --target web -t filemanager-web .   веб-интерфейс

# --- Стадия 1: сборка ---
# Компиляция в байт-код заранее: синтаксическая ошибка остановит сборку образа,
# а не запуск контейнера, и интерпретатору не нужно писать .pyc при старте.
FROM python:3.12-slim AS builder
WORKDIR /src
COPY common ./common
COPY server ./server
COPY client ./client
COPY web ./web
RUN python -m compileall -q common server client web

# Права файлов из контекста сборки на Windows приходят как 666: любой
# пользователь контейнера, включая процесс приложения, мог бы переписать код.
# COPY --from сохраняет права, поэтому они выравниваются здесь, один раз:
# каталоги 755, файлы 644 — менять код может только root.
RUN find /src -type d -exec chmod 755 {} + \
 && find /src -type f -exec chmod 644 {} +

# --- Стадия 2: веб-интерфейс ---
# Шлюз HTTP -> TCP и статика страницы. Кода сервера и базы в образе нет:
# шлюз говорит с сервером только по сети, тем же протоколом, что и клиент.
FROM python:3.12-slim AS web

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder --chown=root:root /src/common /app/common
COPY --from=builder --chown=root:root /src/client /app/client
COPY --from=builder --chown=root:root /src/web /app/web
USER app

# Внутри контейнера шлюз слушает все интерфейсы, наружу порт публикуется
# только на 127.0.0.1. Сервер ищется по имени контейнера в общей сети Docker.
ENV FM_WEB_HOST=0.0.0.0 \
    FM_WEB_PORT=8080 \
    FM_SERVER_HOST=filemanager \
    FM_SERVER_PORT=9000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# /healthz отвечает 200, только если шлюз достаёт до сервера.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["python", "-m", "web.main"]

# --- Стадия 3: сервер (итоговый образ по умолчанию) ---
FROM python:3.12-slim

# Отдельный пользователь без прав root: если сервер взломают, атакующий
# окажется в контейнере без прав администратора.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=root:root /src/common /app/common
COPY --from=builder --chown=root:root /src/server /app/server

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
