# Архитектура

## Процессы одной сессии

```
tmux sovet-<папка>-<хэш>
├─ окно agents (@sovet_window=agents, remain-on-exit, pane-border-status)
│  ├─ панель @sovet_role=codex   sovet native codex  → codex  (оригинальный TUI)
│  │                                                   └─ sovet mcp --native --read-only
│  └─ панель @sovet_role=claude  sovet native claude → claude (оригинальный TUI)
│                                                      └─ sovet mcp --native
├─ окно watch     sovet watch      правки файлов и комментарии chitalka → журнал/очередь
└─ окно chitalka  sovet reader     нативная chitalka (если включена)
```

- `sovet native ROLE` (`cli.py`) ждёт, пока предыдущий процесс роли освободит блокировку
  `worker-ROLE.lock`, затем вызывает `sovet.native.run_native`. Тот запускает оригинальный
  CLI на унаследованном терминале с MCP-конфигом, хуками и инструкциями sovet и
  регистрирует его как `native:ROLE`. Невидимых headless-исполнителей для открытой
  роли нет. `sovet run ROLE` откажется, если роль открыта.
- Хуки (`sovet hook --role R --event E`, `bridge.py`). Claude Code получает их через
  `--settings`, Codex — через `-c hooks.*`. Codex исполняет их только после одобрения
  в его штатном `/hooks`; до этого остаются MCP `memory_read`/`inbox`. Команды хуков
  стабильны между запусками, поэтому одобрение сохраняется.
  - `SessionStart` привязывает ID разговора и передаёт общую память;
  - `UserPromptSubmit` записывает ваше сообщение в журнал, передаёт новые события и
    берёт следующую задачу очереди;
  - `Stop` сохраняет ответ как результат задачи (или как событие). Если есть ждущая
    задача, ход продолжается с ней;
  - `SessionEnd` отмечает отключение.
- Панели находятся по пользовательским опциям tmux (`@sovet_role`, `@sovet_window`)
  и по ID (`%N`, `@N`), а не по номерам. Поэтому `base-index`/`pane-base-index` не важны.
  Подписи хранятся в `@sovet_label`; заголовки, которые выставляют сами TUI, их не
  перезаписывают.
- Каждое окно сначала создаётся с заглушкой `sleep`, затем включается `remain-on-exit`,
  и только потом `respawn-pane` запускает настоящую программу. Так сразу упавший агент
  оставляет на экране свою ошибку, а раскладка не схлопывается.

## Повторный `sovet up`

| Состояние панели | Действие |
|---|---|
| живой `native` | не трогается (кроме `--restart`) |
| завершилась (dead) | `respawn-pane` с тем же разговором |
| отсутствует | `split-window` с нужной стороны |
| старая `panel` (curses) | `respawn-pane` на месте; если по очереди идёт `running`-задача, отказ без `--restart` |

После этого обновляются подписи, `select-layout even-horizontal`, фокус — на писателе.
Изменения касаются только сессии этого проекта.

## Состояние

| Файл | Содержимое |
|---|---|
| `.team/config.json` | профиль, писатель, модели/effort, chitalka (`reader`, `reader_options`) |
| `.team/{brief,state,decisions}.md` | общая память |
| `.team/state.sqlite3` | события, задачи, ID разговоров, курсоры прочитанного (WAL) |
| `.team/native/` | сгенерированные MCP-конфиги, хуки и инструкции для нативных CLI |
| `.team/verification/` | результаты Lean/Comparator |
| `~/.local/state/sovet/sessions/` | реестр PID с проверкой подлинности процесса |

Разговоры хранит сам Codex или Claude Code. sovet хранит только их ID для `resume`
(metadata `native:ROLE:session`). ID пакетных разговоров старых curses-панелей сохранены
как `legacy:ROLE:session`. При первом нативном запуске они не подгружаются:
агент начинает новый нативный разговор.
