# sovet

Два сохраняемых агентских чата в tmux: **Codex слева проверяет, Claude справа меняет файлы**.
Общая память, очередь поручений, независимое ревью, редактирование автором, ARIS,
нативная chitalka и Lean/LongTails для статьи по оптимизации.

Python 3.11+, без Python-зависимостей. Используются установленные `codex`, `claude`,
`tmux` и их обычная авторизация. Отдельные API-ключи sovet не нужны.

## Установка

```sh
cd ~/code/sovet
chmod +x ~/code/sovet/bin/sovet
mkdir -p ~/.local/bin
ln -s ~/code/sovet/bin/sovet ~/.local/bin/sovet
sovet doctor
```

На текущей машине launcher уже установлен в `~/.local/bin/sovet`.
Альтернатива: `uv tool install -e ~/code/sovet`.
Исходная chitalka найдена в `~/code/python/chitalka`, её команда уже установлена.

## Статья по оптимизации

```sh
cd ~/path/to/paper
sovet init --math-research --brief 'Точная постановка задачи и целевой результат'
sovet aris --repo ~/code/aris
sovet research setup --source ~/code/longtails --fetch-cache
sovet up --math-research --reader --latex-watch
```

`--source` использует локальный LongTails как источник **отдельной рабочей копии**
в `formal/longtails/`. Без `--source` setup клонирует upstream. Он сохраняет commit,
`lean-toolchain` и хэш `lake-manifest.json` в `.team/longtails.lock.json`.
Существующий checkout не сбрасывается, версии зависимостей не обновляются.
`--fetch-cache` запускает штатный `lake exe cache get`; при первом запуске Elan
может загрузить нужный Lean, Lake — библиотеки и готовые артефакты. Это отдельный,
потенциально длительный этап. Полная сборка LongTails при открытии чатов не запускается.

Готовый пример без математических утверждений:

```sh
cd ~/code/sovet/examples/optimization-paper
sovet up --reader --latex-watch
```

`chitalka` открывает `main.pdf` или `build/main.pdf`, следит за LaTeX и сохраняет
авторские выделения/комментарии. Наблюдатель sovet раз в секунду:

1. Обнаруживает новый комментарий или уточнение автора и фиксирует его для обеих сессий.
2. Ставит задачу исполнителю Claude, включая цитату, предложение и `source.file:line`.
3. Агент правит исходник, проверяет PDF и отвечает инструментом `chitalka_reply`.
4. Ответ появляется в той же карточке. Ответы ИИ не создают новые задачи сами на себя.

Для управления без браузера:

```sh
sovet chitalka list --json
sovet chitalka show COMMENT_ID
sovet chitalka reply COMMENT_ID 'Исправлено main.tex:42' --done
sovet reader build/main.pdf --watch
```

Записи комментариев меняет только публичная CLI chitalka с её файловыми блокировками.
Sovet не переписывает формат комментариев и не удаляет авторские сообщения.
`--reader` нужен для запуска reader-сервиса; математические инструменты работают и без браузера.
`--no-browser` полезен при запуске `up` по SSH или в тесте.

## Lean и LongTails

Оба агента в математическом профиле получают MCP-инструменты:

| Инструмент | Действие |
|---|---|
| `lean_search` | Поиск в исходниках LongTails и скачанном Mathlib |
| `lean_check` | `lake env lean --json FILE`, диагностика компилятора |
| `lean_build` | Штатный `build.sh MODULE` LongTails: кэш, индекс импортов, сборка |
| `longtails_audit` | `python3 run_comparator.py Theorem.lean` |
| `lean_status` | Проверка актуальности последнего результата относительно исходников |

Те же действия доступны человеку:

```sh
sovet research search ConvexOn
sovet research check LongTails/Theorems/AcceleratedConvex.lean
sovet research build LongTails.Theorems.AcceleratedConvex
sovet research audit AcceleratedConvex.lean
sovet research status
```

Проверки сохраняют команду, код выхода, диагностику и хэш исходников в
`.team/verification/latest.json` и общей истории. Изменение исходников после проверки
делает её устаревшей. Изменение во время проверки не помечается успешной верификацией.
Компиляция и Comparator-аудит явно различаются; совпадение формальной постановки
с задачей автора дополнительно проверяется при ревью. Генерируемые Comparator challenge
могут содержать `sorry`; в решении, публичных доказательствах и новых аксиомах их быть не должно.

`.team/claims.json` связывает утверждения статьи с постановкой, теоремой, бюджетом
и статусом проверки. Это рабочий реестр для автора/агентов; sovet не объявляет его
содержимое математически доказанным автоматически.

## ARIS

Источник: [Auto-claude-code-research-in-sleep](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep).
При `sovet aris --repo PATH` регистрируется commit и подключаются оригинальные навыки:
`research-wiki`, `research-refine`, `auto-review-loop`, `lean-formalize`, `proof-checker`.
В `--math-research` создаются именованные symlink в `.agents/skills/` и `.claude/skills/`,
проектный `.aris/tools`, а официальный `tools/research_wiki.py` инициализирует wiki.
Установленные навыки пользователя и AGENTS.md/CLAUDE.md не переписываются.

Транспорт ревью адаптирован: поручения идут через `ask_peer` в те же сохраняемые
сессии sovet. Встроенные в upstream ARIS выборы Astra/ultra и отдельные nested CLI
не используются; действуют выбранные в sovet модели/effort. Это интеграция методики
и оригинальных инструментов ARIS, а не заявление о прохождении всех upstream review gates.

Для новой машины ARIS можно скачать отдельно:

```sh
git clone --depth 1 https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep.git ~/code/aris
sovet aris --repo ~/code/aris
```

## Программирование и настройки моделей

```sh
cd ~/path/to/code
sovet init --programming
sovet up --programming
```

В programming-профиле не объявляются Lean-инструменты; без `--reader` не объявляются
инструменты chitalka. Управляемые sovet symlink исследовательских навыков ARIS снимаются;
wiki, доказательства и история сохраняются. Переключение профиля применяется к новым ходам.

По умолчанию:

| Агент | Модель | Effort | Роль |
|---|---|---|---|
| Claude | `claude-opus-5-5` | `high` | исполнитель |
| Codex | `gpt-6.1-sol` | `high` | ревьюер |

Переопределить при init/up или отдельно:

```sh
sovet up --math-research \
  --claude-model claude-opus-5-5 --claude-effort high \
  --codex-model gpt-6.1-sol --codex-effort xhigh

sovet config --codex-effort high
sovet config --writer codex
sovet config
```

Флаги сохраняются **для этого проекта** в `.team/config.json`, глобальные настройки
Codex/Claude не меняются. Модель передаётся как точный ID, без скрытого fallback.
Поддержка модели/effort проверяется самим провайдером при запуске; doctor не подтверждает
доступ по подписке. Смена модели сохраняет историю соответствующей нативной сессии.

## Два чата и общая память

Панели — собственный терминальный интерфейс sovet поверх штатных `codex exec --json`
и `claude -p --output-format stream-json`. Это не зеркалирование двух оригинальных TUI.
У каждого агента свой нативный ID сессии; повторный ход использует явный `resume` этого ID.
Sovet не подключается к произвольному уже запущенному терминальному чату вне sovet.

- Сообщение + Enter — задача текущему агенту.
- `/ask TEXT` — ваше поручение в другой чат.
- `/sync` — попросить обработать новые события/правки.
- `/status` — последние задачи; `/quit` — закрыть панель.
- Ctrl+C — отменить текущий ход, PgUp/PgDn — прокрутка.
- tmux: Ctrl+b и стрелка — соседняя панель, Ctrl+b d — отсоединиться.

```sh
sovet send claude 'Исправь доказательство в main.tex и попроси Codex проверить'
sovet review 'Проверь гипотезы, шаги доказательства и подсчёт oracle calls'
sovet status
sovet events
sovet run claude                 # один queued job без tmux
sovet up --detach                # workers продолжают работать без открытого терминала
sovet stop
```

Список **всех запущенных** сессий доступен из любой папки:

```sh
sovet sessions                     # или sovet ps
sovet sessions --json
sovet -C ~/path/to/paper sessions   # только этот проект
sovet stop sovet-paper-ABCDEF       # ID из списка; вместо ID можно PID
sovet stop                         # все процессы текущего проекта
sovet stop --all                    # все сессии sovet этого пользователя
sovet stop --all --force            # SIGKILL оставшихся после SIGTERM и ожидания
```

Источником истины служит `ps`, а реестр `~/.local/state/sovet/sessions/`
(`$XDG_STATE_HOME` также поддерживается) добавляет метаданные проекта и помогает найти
осиротевшие процессы моделей. Старые CLI-процессы обнаруживаются без реестра;
их рабочая папка определяется через `/proc` или `lsof`.
Остановка учитывает дочерние процессы, включая отдельные process groups: модели, MCP,
читалку и сборщики. Перед каждым сигналом повторно проверяются UID, время запуска и
команда процесса. Без доступа к `ps` команда завершится ошибкой и не станет угадывать PID.
Посторонние сессии Claude/Codex, запущенные вне sovet, в выборку не входят.
История, комментарии и ожидающие задачи не удаляются.

Для независимых читалок: `chitalka sessions`, `chitalka stop PID`, `chitalka stop --all`.

## Tab-автодополнение

```sh
sovet completion zsh --install
chitalka completion zsh --install
# Для Bash аналогично: completion bash --install
```

На текущей машине оба дополнения уже подключены к `~/.zshrc`. В новом терминале
работает Tab для команд, флагов, путей, ролей и effort, ID запущенных сессий и
комментариев chitalka. ID читаются локально, без запросов к моделям.
Повторная установка заменяет только свой помеченный блок в конфиге оболочки.
Для текущего терминала:

```sh
source ~/.local/share/sovet/completion/sovet.zsh
source ~/.local/share/chitalka/completion/chitalka.zsh
```

`ask_peer` асинхронен: агент ставит поручение и завершает свой ход, ответ коллеги
возвращается отдельным ходом. Это устраняет взаимное ожидание. Глубина цепочки ограничена;
получение результата не запускает бесконечный цикл ревью. Ошибки остаются failed-задачами,
частично исполненная задача после аварии автоматически не повторяется.

Общая память: `.team/brief.md`, `state.md`, `decisions.md`; события, задачи и ID сессий —
`.team/state.sqlite3` с транзакциями и WAL. Новые события подаются в контекст перед ходом;
курсор прочитанного обновляется после успешного ответа. История разговора и приватные
рассуждения моделей не сливаются в единый скрытый контекст.

## Правки автора

Редактируйте `.tex`, код и Markdown привычным редактором, через chitalka или командой:

```sh
sovet edit main.tex
sovet edit .team/brief.md
sovet memory decisions 'Фиксируем точный стохастический oracle model' --append
```

Watcher фиксирует diff и уведомляет обе сессии. При внешнем редактировании принадлежность
правки неизвестна; `sovet edit` и chitalka явно помечают её авторской. Поступившие во время
работы изменения доставляются **перед следующим ходом**, без небезопасной вставки текста в TUI.
В programming-профиле обычные файловые изменения не запускают новые модельные ответы
автоматически; используйте `/sync`. Комментарии chitalka автоматически ставят задачу исполнителю
(`auto_comments: false` в config отключает это).
Для больших текстовых файлов проверяется хэш всего файла, а в историю помещается только
ограниченный фрагмент diff; полное содержимое агент перечитывает по пути.

Для редактируемых результатов MCP `artifact_write` требует SHA прочитанной версии и
отклоняет обнаруженную устаревшую запись. Эта защита действует для записей через sovet;
прямые Edit/Bash и внешние редакторы не используют его блокировку, поэтому агенты также
получают правило перечитывать файлы и сохранять авторские правки.

## Проверка разработки

```sh
cd ~/code/sovet
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/live_smoke.py  # opt-in: реальные запросы обоим провайдерам
PYTHONPATH=. python3 tests/live_pipeline.py  # opt-in: запись -> ревью -> обратная доставка
PYTHONPATH=. python3 tests/live_sessions.py  # opt-in: ps, остановка только созданных тестовых процессов
```

Unit-тесты не вызывают модели. Live smoke использует CLI-авторизацию и делает по два
коротких хода каждому агенту, проверяя MCP и сохранение ID сессии.
Журналы реальных запусков находятся в `.team/logs/`. Повторная полная сборка всех
теорем LongTails не является частью тестов самого sovet.

Исходники интеграций: [chitalka](https://github.com/chitoraphelion/chitalka),
[LongTails](https://github.com/k3nfalt/longtails),
[Lake](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/),
[Codex automation](https://learn.chatgpt.com/docs/codex-sdk),
[Claude automation](https://code.claude.com/docs/en/headless).
