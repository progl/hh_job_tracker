CREATE TABLE IF NOT EXISTS vacancies (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    company_id      INTEGER,
    company_name    TEXT,
    area_id         INTEGER,
    area_name       TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    salary_currency TEXT,
    salary_gross    BOOLEAN,
    salary_rub      INTEGER,
    work_schedule   TEXT,
    employment      TEXT,
    work_experience TEXT,
    work_formats    TEXT,
    publication_time TEXT,
    creation_time   TEXT,
    is_remote       BOOLEAN,
    is_remote_text  BOOLEAN,
    level           TEXT,
    key_skills      TEXT,
    parsed_stack    TEXT,
    responses_count INTEGER,
    total_responses_count INTEGER,
    online_users_count INTEGER,
    description     TEXT,
    raw_json        TEXT,
    url             TEXT,
    archived_at     TEXT,
    seen_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vacancy_status (
    vacancy_id   INTEGER PRIMARY KEY REFERENCES vacancies(id),
    status       TEXT NOT NULL DEFAULT 'new',
    note         TEXT,
    tags         TEXT,
    rating       INTEGER,
    applied_at   TEXT,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS searches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    params       TEXT NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employers (
    id                 INTEGER PRIMARY KEY,
    name               TEXT,
    is_accredited_it   BOOLEAN,
    all_topic_count    INTEGER,
    read_topic_percent INTEGER,
    reply_working_days REAL,
    raw_json           TEXT,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS negotiations (
    id                    INTEGER PRIMARY KEY,
    vacancy_id            INTEGER,
    employer_id           INTEGER,
    employer_manager_id   INTEGER,
    resume_id             INTEGER,
    last_state            TEXT,
    last_employer_state   TEXT,
    applicant_sub_state   TEXT,
    employer_sub_state    TEXT,
    initial_topic_type    TEXT,
    current_topic_type    TEXT,
    archived              BOOLEAN,
    declined_by_applicant BOOLEAN,
    viewed_by_opponent    BOOLEAN,
    has_new_messages      BOOLEAN,
    has_response_letter   BOOLEAN,
    conversation_messages INTEGER,
    creation_time         TEXT,
    last_modified         TEXT,
    raw_json              TEXT,
    seen_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS status_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id      INTEGER NOT NULL,
    last_employer_state TEXT,
    viewed_by_opponent  BOOLEAN,
    archived            BOOLEAN,
    snapshot_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile (
    id                   INTEGER PRIMARY KEY CHECK(id=1),
    resume_id            TEXT,
    hhid                 TEXT,
    full_name            TEXT,
    title                TEXT,
    years_experience     REAL,
    salary_expected_from INTEGER,
    salary_currency      TEXT,
    skills               TEXT,
    formats              TEXT,
    raw_resume           TEXT,
    updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_vacancy_seen (
    search_id    INTEGER NOT NULL,
    vacancy_id   INTEGER NOT NULL,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (search_id, vacancy_id)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    duration_ms INTEGER,
    status      TEXT NOT NULL DEFAULT 'running',
    trigger     TEXT,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, started_at);

CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    method      TEXT NOT NULL DEFAULT 'GET',
    path        TEXT NOT NULL,
    params      TEXT,
    status      INTEGER,
    duration_ms INTEGER,
    size_bytes  INTEGER,
    referer     TEXT,
    redirect_to TEXT,
    error       TEXT,
    kind        TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_logs_ts ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_request_logs_status ON request_logs(status);

CREATE TABLE IF NOT EXISTS vacancy_collected_via (
    vacancy_id     INTEGER NOT NULL,
    query_text     TEXT NOT NULL DEFAULT '',
    area           TEXT NOT NULL DEFAULT '',
    schedule       TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (vacancy_id, query_text, area, schedule)
);

CREATE TABLE IF NOT EXISTS cookie_store (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vacancies_company ON vacancies(company_id);
CREATE INDEX IF NOT EXISTS idx_vacancies_area ON vacancies(area_id);
CREATE INDEX IF NOT EXISTS idx_vacancies_seen ON vacancies(seen_at);
CREATE INDEX IF NOT EXISTS idx_negotiations_vacancy ON negotiations(vacancy_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_neg ON status_snapshots(negotiation_id, snapshot_at);

-- LLM pipeline: лог каждого вызова модели (что дали / что вернула)
CREATE TABLE IF NOT EXISTS llm_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_kind       TEXT NOT NULL,                         -- requirements | ...
    target_kind     TEXT,                                  -- vacancy | resume | ...
    target_id       TEXT,                                  -- "12345"
    model           TEXT NOT NULL,                         -- qwen3:14b
    prompt_version  TEXT NOT NULL,                         -- requirements_v1
    system_prompt   TEXT,
    user_prompt     TEXT,
    response_raw    TEXT,
    parsed_json     TEXT,                                  -- результат JSON-парсинга или NULL
    ok              INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    latency_ms      INTEGER,
    prompt_tokens   INTEGER,
    response_tokens INTEGER,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_runs_target ON llm_runs(target_kind, target_id);
CREATE INDEX IF NOT EXISTS idx_llm_runs_created ON llm_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_runs_task ON llm_runs(task_kind, ok);

-- Универсальное хранилище результатов LLM-анализа (один результат на (vacancy, kind))
-- Для kind='requirements' список идёт в отдельную таблицу vacancy_requirements (см. ниже),
-- здесь хранятся «однообъектные» анализы: salary, company_kind, summary, …
CREATE TABLE IF NOT EXISTS vacancy_analysis (
    vacancy_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL,           -- salary | company_kind | summary | ...
    data_json    TEXT NOT NULL,           -- результат анализа в JSON
    llm_run_id   INTEGER,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (vacancy_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_vacancy_analysis_kind ON vacancy_analysis(kind);

-- Извлечённые из описаний требования
CREATE TABLE IF NOT EXISTS vacancy_requirements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL,                            -- must | nice | plus
    category     TEXT,                                     -- stack | exp | soft | edu | other
    text         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'llm',              -- llm | regex | manual
    llm_run_id   INTEGER,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vacancy_id, kind, text)
);
CREATE INDEX IF NOT EXISTS idx_vacancy_requirements_vid ON vacancy_requirements(vacancy_id);
CREATE INDEX IF NOT EXISTS idx_vacancy_requirements_cat ON vacancy_requirements(category, kind);
