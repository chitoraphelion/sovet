# sovet: команды подробно

Краткое введение — в [README](../README.md). Здесь собраны детали для повседневной работы.
Блоки «Синтаксис» описывают аргументы (`[…]` — необязательно, `A | B` — одно из двух);
копировать нужно примеры в блоках `sh`.

## Создание проекта

Синтаксис:

```text
sovet init [PATH] (--programming | --math-research) [--brief TEXT] [--writer claude|codex]
           [--aris DIR] [--codex-model ID] [--codex-effort E] [--claude-model ID] [--claude-effort E]
```

```sh
sovet init --programming
sovet init ~/papers/stochastic --math-research --brief 'Точная постановка и целевой результат'
sovet init --programming --writer codex --codex-effort xhigh
```

`init` создаёт `.team/` и не перезаписывает существующую память. Если в `~/code/aris`
есть checkout ARIS, навыки подключаются автоматически.

## Запуск и сессия tmux

| Команда | Действие |
|---|---|
| `sovet up` | создать сессию или вернуться в неё; упавшие или недостающие панели запустить снова |
| `sovet up --detach` | то же без подключения (по SSH, из скрипта) |
| `sovet up --restart` | перезапустить агентов, `watch` и `chitalka` этой сессии |
| `sovet up --dry-run` | вывести команды tmux; ни сессию, ни конфиг не менять |
| `sovet up --reader --pdf build/main.pdf --latex-watch` | добавить окно chitalka; `--pdf` и `--latex-watch` запоминаются |
| `sovet up --reader --no-browser` | chitalka без открытия браузера (SSH) |
| `sovet status` | живые агенты (PID), tmux, службы, очередь |
| `sovet status --json` | то же для скриптов |

Имя tmux-сессии: `sovet-<папка>-<хэш пути>`. У каждого проекта своя сессия.
Окно `agents` всегда содержит Codex слева и Claude справа, `even-horizontal`.
Новое окно создаётся точно по размеру вашего терминала. Без терминала (`--detach`,
скрипт без TTY) размер 160×45; при подключении tmux подгонит окно под ваш экран.
Из скрипта без TTY `up` работает как `--detach`.

## Очередь поручений

```sh
sovet send claude 'Сократи введение до одной страницы'   # поручение конкретному агенту
sovet send codex 'Проверь лемму 3' --read-only
git diff | sovet send codex -                              # текст из stdin
sovet review                                               # ревьюеру: проверить текущие изменения
sovet review 'Проверь подсчёт oracle calls' --json         # ответ вернётся писателю
sovet events                                               # общая история
sovet events --after 120 --json
sovet run codex                                            # одна задача headless, без TUI
```

`sovet run` работает, только если этот агент не открыт в tmux: два процесса в одном
разговоре не допускаются.

Статусы задач: `ждёт` → `выполняется` → `готово` / `ошибка`. Когда и как задача
доставляется, описано в разделе README «Как проходит ревью».

## Обрывы соединения Codex

Если в журнале повторяются `websocket closed by server` / `Connection reset by peer`,
для проекта с обычным входом Codex через ChatGPT можно выбрать HTTP/SSE:

```sh
sovet config --codex-transport http
# вернуть штатный выбор транспорта:
sovet config --codex-transport auto
```

Настройка применяется к следующим headless/TUI-запускам этого проекта; текущий процесс
нужно завершить с сохранением состояния и продолжить. Используется именованный provider
`sovet_http` с тем же ChatGPT backend и существующей авторизацией Codex. Модель, reasoning,
conversation ID и sandbox сохраняются; глобальный config Codex не переписывается.
Этот вариант предназначен для ChatGPT login, не для стороннего API-provider.

[Официальная настройка `supports_websockets`](https://learn.chatgpt.com/docs/config-file/config-reference)
управляет транспортом Responses. Встроенный provider `openai` не переопределяется;
для HTTP создаётся отдельный ID. Переключение транспорта само по себе не гарантирует
доступность сервиса; ограниченные сетевые повторы продолжают действовать.

## Ночной цикл без присмотра

```sh
sovet night --task RESEARCH_BRIEF.md --hours 8 --max-turns 32 --dry-run
sovet night --task RESEARCH_BRIEF.md --hours 8 --max-turns 32
```

Задание — непустой UTF-8 файл внутри проекта, он перечитывается при продолжении работы.
При пустой очереди писатель получает следующий шаг по этому заданию; после его результата
координатор ставит независимое ревью, если ревью уже не запросил сам писатель. Ответ
ревьюера возвращается писателю через штатную очередь. Модели и reasoning — из `sovet config`.
Изменения автора и агентские замечания chitalka подхватываются перед очередным ходом.
Личные N-заметки не подхватываются. `--dry-run` не запускает модели и не меняет очередь.

Обе роли заняты на весь цикл: `sovet up` / `sovet run` в этом же проекте параллельно
не запускайте. Две headless-разговорные сессии сохраняются отдельно от нативных TUI-сессий;
у обеих форм работы общая память и файлы. `sovet up` после ночи читает эти результаты.

Сетевые обрывы повторяются с задержками 15/30/60 секунд (до 3 повторов на задачу).
Таймаут отдельного хода ставит в очередь ограниченное продолжение: только восстановить
checkpoint, отчёт и следующий шаг, затем завершить ход. Частичные правки не откатываются,
неуспешный ход не засчитывается как доказательство. Повторы таймаута также ограничены
`--retries` и общими лимитами времени/ходов.
`--retries N` меняет предел, `--retries 0` отключает восстановление. Отклонённый лимит
Claude с явным `resetsAt` переводит цикл в ожидание до сброса + 60 секунд. Предупреждение
о приближении лимита не считается отказом; неизвестное время сброса не угадывается.
Ожидание входит в `--hours`, каждая модельная попытка — в `--max-turns`.

Повтор продолжает ту же сессию и перечитывает частичные правки. Роль, read-only,
глубина и адресат ревью сохраняются; старые задачи/логи остаются failed для аудита,
продолжение получает новый ID. Во время ожидания `.team/night/latest.json` содержит
`reason=waiting_retry`, `resume_at` и `pending_job`. Ctrl+C / `sovet stop` прерывают
ожидание без потери этой pending-задачи.

Остановка: Ctrl+C, `sovet stop`, неповторяемая ошибка, исчерпание повторов, `--hours` или `--max-turns`.
В `.team/night/latest.json` записывается причина и ID оставшейся задачи, в `.team/logs/`
лежат исходные журналы. Ошибочная задача остаётся failed: перед новым запуском прочитайте
ошибку и файлы. Running-задачи после аварийного SIGKILL автоматически не повторяются;
проверьте `sovet status` и при необходимости завершите их через штатный механизм очереди.
Существующие pending-задачи новый запуск исполняет раньше нового исследовательского шага.

### Временно недоступный ревьюер

```sh
sovet night --reviewer-after '2026-10-17T20:31:00+03:00' --hours 8 --max-turns 32
```

До указанного времени CLI ревьюера не вызывается. Ведущий продолжает ограниченные
задачи; одно ожидающее ревью не дублируется после каждого хода. После восстановления
доступа ревью получает актуальный checkpoint на следующей границе хода. При известном
сбросе лимита ревьюера его повтор откладывается, а ведущий продолжает работать.
`--hours` включает ожидание. Последние два доступных хода по возможности резервируются
для независимого ревью и ответа ведущего; если до сброса не хватает времени, ожидающая
задача сохраняется. Непроверенные результаты должны оставаться явно непроверенными.

Смена ведущего выполняется только между запусками, когда роли свободны:
`sovet config --writer codex` (или `claude`). Сначала сохранить checkpoint, затем
передать незавершённые задачи с правильными правами и адресатом ответа. Простая смена
`writer` во время работающего night останавливает старый координатор. История обеих
сессий сохраняется. Если ревьюер начинает отдельную компактную сессию, архивируйте
предыдущий ID, сохраняя общую память и ссылки на актуальные файлы.

## chitalka

```sh
sovet up --reader --latex-watch       # окно chitalka в tmux
sovet reader build/main.pdf --watch   # то же в текущем терминале
sovet reader notes.md                # Markdown с KaTeX, обновляется без latexmk
sovet up --reader --document notes.md # тот же документ при следующих запусках
sovet chitalka list --json            # любая команда chitalka для этого проекта
sovet chitalka show COMMENT_ID
sovet chitalka reply COMMENT_ID 'Исправлено main.tex:42' --done
```

Файл комментариев меняет только CLI chitalka со своими блокировками. sovet не
переписывает его формат и не удаляет сообщения автора. Повторная правка или уточнение
автора в той же карточке снова создаёт задачу писателю; ответы ИИ задач не создают.
Личные N-заметки chitalka (`*.notes.json`) исключены из наблюдения файлов и автоматического
контекста. Агенты проинструктированы не читать их и не включать в общую память или задачи.

## Lean и LongTails (`--math-research`)

```sh
sovet research setup --source ~/code/longtails --fetch-cache
sovet research search ConvexOn
sovet research check LongTails/Theorems/AcceleratedConvex.lean
sovet research build LongTails.Theorems.AcceleratedConvex
sovet research audit AcceleratedConvex.lean
sovet research status
```

`setup --source` создаёт **отдельную рабочую копию** локального LongTails в `formal/longtails/`.
Без `--source` setup клонирует upstream. Commit, `lean-toolchain` и хэш `lake-manifest.json`
записываются в `.team/longtails.lock.json`. Существующий checkout не сбрасывается.
`--fetch-cache` запускает `lake exe cache get`. Первый раз это долго: Elan загружает Lean,
Lake — Mathlib и готовые артефакты. `sovet up` полную сборку не запускает.

Те же действия агенты выполняют MCP-инструментами:

| Инструмент | Действие |
|---|---|
| `lean_search` | поиск в исходниках LongTails и Mathlib |
| `lean_check` | `lake env lean --json FILE` |
| `lean_build` | штатный `build.sh MODULE` LongTails |
| `longtails_audit` | `python3 run_comparator.py Theorem.lean` |
| `lean_status` | актуален ли последний результат относительно исходников |

Результат сохраняется в `.team/verification/latest.json`: команда, код выхода, диагностика
и хэш исходников. Правка исходников после проверки делает её устаревшей. Компиляция и
аудит Comparator — разные проверки. Совпадение формальной постановки с задачей автора
проверяется на ревью. В challenge-файлах Comparator допустим `sorry`; в решениях,
публичных доказательствах и новых аксиомах — нет.

## ARIS

```sh
git clone --depth 1 https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep.git ~/code/aris
sovet aris --repo ~/code/aris
```

Регистрируется commit и подключаются оригинальные навыки `research-wiki`,
`research-refine`, `auto-review-loop`, `lean-formalize`, `proof-checker`. Они ставятся
как symlink в `.agents/skills/` и `.claude/skills/`, плюс `.aris/tools`.
Ваши навыки и AGENTS.md/CLAUDE.md не переписываются. Межагентное ревью ARIS идёт через
`ask_peer` к агенту из соседней панели, а не через вложенные CLI. Поэтому действуют модели
и effort из sovet. Это интеграция методики и инструментов ARIS; прохождение всех upstream
review gates sovet не гарантирует.

## Сессии и остановка

```sh
sovet sessions                   # или sovet ps; из любой папки
sovet sessions --json
sovet -C ~/paper sessions        # только этот проект
sovet stop                       # всё, что sovet запустил для текущего проекта
sovet stop sovet-optimization-paper-3818f4       # по ID из sovet sessions
sovet stop 51732 --timeout 3 --force             # по PID; SIGKILL после 3 с
sovet stop --all                 # все сессии sovet этого пользователя
```

Процессы находятся через `ps` и реестр `~/.local/state/sovet/sessions/`
(учитывается `$XDG_STATE_HOME`). Останавливаются и дочерние процессы: MCP, chitalka,
latexmk, сборки. История, комментарии, очередь и память сохраняются.
Для отдельно запущенной читалки: `chitalka sessions`, `chitalka stop PID`.

## Правки и артефакты

```sh
sovet edit main.tex                         # $VISUAL/$EDITOR, правка помечается авторской
sovet artifact read main.tex                # текст + sha
sovet artifact write main.tex --expected-sha SHA --text -   # отказ при конфликте версий
```
