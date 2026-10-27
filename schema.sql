-- Toca da Capivara: esquema da base de dados (SQLite).
-- O main.py lê este ficheiro no arranque e cria o que faltar (não apaga dados).
-- O ficheiro da base é o toca.db, ao lado do main.py.
PRAGMA foreign_keys = ON;

-- Contas. "key" é o nome em minúsculas (garante que Ana e ana não coexistem).
-- "pw" guarda salt$hash (scrypt); a palavra-passe em si nunca é guardada.
CREATE TABLE
    IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        Key TEXT NOT NULL UNIQUE,
        pw TEXT NOT NULL,
        created REAL NOT NULL -- segundos desde 1970
    );

-- Sessões iniciadas (cookie). Guarda só o hash do token.
CREATE TABLE
    IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created REAL NOT NULL
    );

-- Museu dos Vídeos: um registo por descarga concluída.
CREATE TABLE
    IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        title TEXT,
        url TEXT, -- link original
        thumbnail TEXT,
        site TEXT, -- YouTube, TikTok, Instagram...
        uploader TEXT,
        duration REAL, -- segundos
        kind TEXT, -- 'video' ou 'audio'
        height INTEGER, -- 1080, 720... (vazio no áudio)
        ext TEXT, -- mp4, mp3, m4a...
        created REAL NOT NULL,
        file TEXT -- cópia guardada na pasta videos/ (para ver no museu)
    );

CREATE INDEX IF NOT EXISTS idx_history_user ON history (user_id, created);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);