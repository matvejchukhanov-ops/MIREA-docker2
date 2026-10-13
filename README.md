# Локальный CI/CD для файлового менеджера

Учебный проект по дисциплине «Разработка безопасного программного обеспечения»,
семестровое задание № 2. Тестовое приложение — сервер и клиент файлового
менеджера из задания № 1. Для него настроен конвейер непрерывной интеграции и
доставки, который целиком работает на компьютере студента: утилита
[act](https://nektosact.com/) исполняет workflow GitHub Actions в локальных
Docker-контейнерах.

## Конвейер

```
build ──► test ──► sast ──► deploy
  │         │        │         │
  ▼         ▼        ▼         ▼
src.tar.gz  junit.xml   semgrep.json  image.tar.gz
            coverage.xml              + контейнер filemanager на 127.0.0.1:9000
```

| Стадия | Что делает | Артефакт |
|---|---|---|
| `build` | Компилирует весь код в байт-код, упаковывает исходники | `filemanager-src.tar.gz` |
| `test` | Прогоняет тесты через pytest, считает покрытие | `junit.xml` (JUnit), `coverage.xml` (Cobertura) |
| `sast` | Semgrep с правилами `p/python` и `p/secrets`; находка уровня ERROR останавливает конвейер | `semgrep.json` |
| `deploy` | Собирает образ, запускает контейнер, проверяет его smoke-тестом | `image.tar.gz` |

## Соответствие требованиям задания

| Требование | Где выполнено |
|---|---|
| Изоляция: каждый шаг в контейнере | act запускает каждое задание в отдельном контейнере `catthehacker/ubuntu:act-latest` |
| SAST с прерыванием сборки | Задание `sast`: `semgrep scan --severity ERROR --error` |
| Секреты не в коде | `SMOKE_PASSWORD` приходит из файла `.secrets` (в `.gitignore`) через `secrets.*` |
| Кэширование зависимостей | `actions/cache` для `~/.cache/pip`, ключ — хэш `requirements-dev.txt` |
| Артефакты | `actions/upload-artifact` в каждой стадии, act складывает их в `.artifacts/` |
| Явные стадии | Задания `build`, `test`, `sast`, `deploy`, порядок задан цепочкой `needs` |
| Multi-stage build | [Dockerfile](Dockerfile): стадия `builder` и итоговый `python:3.12-slim` |
| Непривилегированный пользователь | В образе пользователь `app` (uid 10001), код принадлежит root |

Подробный разбор с теорией и рисками — [docs/ci-cd.md](docs/ci-cd.md).

## Структура

```
.github/workflows/main.yml   конвейер
Dockerfile, .dockerignore    образ сервера
.actrc                       настройки act
.secrets.example             образец файла секретов
requirements-dev.txt         инструменты конвейера: pytest, coverage, semgrep
ci/smoke.py                  проверка развёрнутого сервера
.devcontainer/               одинаковое окружение разработки в VS Code
server/ client/ common/      файловый менеджер (задание № 1)
tests/                       тесты файлового менеджера
docs/ci-cd.md                отчёт
```

## Запуск

Нужны Docker Desktop и act (`winget install nektos.act`).

1. Создать файл секретов по образцу и задать пароль тестового пользователя:
   ```
   copy .secrets.example .secrets
   ```
2. Запустить конвейер из корня репозитория:
   ```
   act push
   ```
   Отдельная стадия: `act -j test`. Список стадий: `act -l`.
3. После успешного прогона сервер работает в контейнере `filemanager`.
   Подключиться клиентом:
   ```
   python -m client.main
   ```

Артефакты лежат в `.artifacts/<номер прогона>/`. Образ из артефакта
загружается на другой машине командой `docker load -i image.tar.gz`.

Без конвейера сервер и клиент запускаются как в задании № 1:
`python -m server.main` и `python -m client.main`, нужен только Python 3.11+.
