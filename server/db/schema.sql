-- Схема базы данных файлового менеджера (SQLite).
--
-- Таблицы users, files, operations повторяют вариант из задания. Отличия
-- от примера для PostgreSQL продиктованы SQLite:
--   * SERIAL      -> INTEGER PRIMARY KEY AUTOINCREMENT;
--   * ENUM        -> TEXT с ограничением CHECK;
--   * хранимые процедуры -> триггеры. В SQLite процедур нет, а триггеры
--     дают то же главное свойство: правило живёт в базе и выполняется при
--     любом изменении данных, а не только когда приложение о нём вспомнило.
--
-- Время хранится в UTC строкой 'ГГГГ-ММ-ДД ЧЧ:ММ:СС.ссс' — такие строки
-- сравниваются как даты обычным строковым сравнением.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COLLATE NOCASE: «Admin» и «admin» — один пользователь, иначе можно
    -- зарегистрировать двойника, неотличимого в журнале на глаз.
    username        TEXT    NOT NULL UNIQUE COLLATE NOCASE
                            CHECK (length(username) BETWEEN 3 AND 32),
    -- Только хеш scrypt с солью. Пароль в открытом виде не хранится нигде.
    password_hash   TEXT    NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    failed_logins   INTEGER NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
    locked_until    TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    modified_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    size            INTEGER NOT NULL CHECK (size >= 0),
    -- Путь внутри каталога владельца, через «/». Абсолютный путь на сервере
    -- в базу не пишется: он не нужен клиенту и раскрывает устройство сервера.
    location        TEXT    NOT NULL CHECK (length(location) BETWEEN 1 AND 1024),
    owner_id        INTEGER NOT NULL REFERENCES users(id),
    -- Удалённый файл помечается, а не стирается: на него ссылаются записи
    -- журнала операций, и история не должна терять концы.
    deleted_at      TEXT
);

-- Живая запись на путь у владельца — одна. Удалённых может быть сколько угодно.
CREATE UNIQUE INDEX IF NOT EXISTS files_live_location
    ON files(owner_id, location) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    operation_type  TEXT    NOT NULL CHECK (operation_type IN ('create', 'modify', 'delete')),
    file_id         INTEGER NOT NULL REFERENCES files(id),
    user_id         INTEGER NOT NULL REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS operations_by_user ON operations(user_id, id);

-- Журнал активности: каждая команда каждого клиента, успешная или нет,
-- включая неудачные попытки входа.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    user_id         INTEGER REFERENCES users(id),
    -- Логин, под которым пытались войти, — даже если такого пользователя нет.
    username        TEXT,
    peer            TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    target          TEXT,
    success         INTEGER NOT NULL CHECK (success IN (0, 1)),
    details         TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_by_user ON audit_log(user_id, id);

-- === Журнал операций ведёт сама база ===
--
-- Приложение меняет только таблицу files. Запись в operations делают
-- триггеры, поэтому изменение файла без записи в журнал невозможно в
-- принципе — даже если в коде забудут об этом или выполнят UPDATE вручную.
-- Действие над файлом выполняет его владелец: доступа к чужим файлам нет.

CREATE TRIGGER IF NOT EXISTS files_log_create
AFTER INSERT ON files
WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO operations (operation_type, file_id, user_id)
    VALUES ('create', NEW.id, NEW.owner_id);
END;

CREATE TRIGGER IF NOT EXISTS files_log_modify
AFTER UPDATE OF size, modified_at ON files
WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NULL
BEGIN
    INSERT INTO operations (operation_type, file_id, user_id)
    VALUES ('modify', NEW.id, NEW.owner_id);
END;

CREATE TRIGGER IF NOT EXISTS files_log_delete
AFTER UPDATE OF deleted_at ON files
WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
BEGIN
    INSERT INTO operations (operation_type, file_id, user_id)
    VALUES ('delete', NEW.id, NEW.owner_id);
END;

-- === Неизменяемость истории ===
--
-- Журналы только дополняются. Злоумышленник, получивший возможность
-- выполнить запрос, первым делом попробует стереть следы; триггеры
-- заставляют его для этого менять саму схему.

CREATE TRIGGER IF NOT EXISTS files_no_delete
BEFORE DELETE ON files
BEGIN
    SELECT RAISE(ABORT, 'Записи о файлах не удаляются: на них ссылается журнал операций');
END;

CREATE TRIGGER IF NOT EXISTS files_identity_immutable
BEFORE UPDATE OF owner_id, location, filename, created_at ON files
BEGIN
    SELECT RAISE(ABORT, 'Владелец, путь и время создания файла не изменяются');
END;

CREATE TRIGGER IF NOT EXISTS files_no_undelete
BEFORE UPDATE OF deleted_at ON files
WHEN OLD.deleted_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Удалённый файл не восстанавливается задним числом');
END;

CREATE TRIGGER IF NOT EXISTS operations_no_update
BEFORE UPDATE ON operations
BEGIN
    SELECT RAISE(ABORT, 'Журнал операций только дополняется');
END;

CREATE TRIGGER IF NOT EXISTS operations_no_delete
BEFORE DELETE ON operations
BEGIN
    SELECT RAISE(ABORT, 'Журнал операций только дополняется');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Журнал аудита только дополняется');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Журнал аудита только дополняется');
END;

-- Представление для чтения журнала операций вместе с именами.
CREATE VIEW IF NOT EXISTS operations_view AS
SELECT o.id, o.timestamp, o.operation_type, o.user_id, u.username,
       f.id AS file_id, f.filename, f.location, f.size
FROM operations o
JOIN files f ON f.id = o.file_id
JOIN users u ON u.id = o.user_id;
