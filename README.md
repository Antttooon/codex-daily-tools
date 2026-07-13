# codex-daily-tools

Локальный набор для ведения Codex daily log и экспорта в Obsidian.

## Что внутри

- `skills/daily-log/SKILL.md` — skill для записи итогов треда в `~/codex-daily/YYYY-MM-DD.md`.
- `scripts/export_codex_daily_to_obsidian.py` — экспорт `~/codex-daily` в Obsidian vault.
- `scripts/backfill_codex_daily_duration.py` — восстановление времени выполнения для старых отчётов.
- `install.sh` — установка skill и exporter на машину пользователя.

## Установка

```bash
./install.sh
```

По умолчанию installer копирует:

- skill в `~/.agents/skills/daily-log`;
- exporter в `~/codex-tools/export_codex_daily_to_obsidian.py`.
- duration backfill в `~/codex-tools/backfill_codex_daily_duration.py`.

## Примеры использования в Codex

Записать итог текущего треда:

```text
$daily-log
```

Записать итог за конкретную дату:

```text
$daily-log --date 2026-07-11
```

Записать итог за вчера:

```text
$daily-log за вчера
```

После большой задачи:

```text
Добавь результат этой задачи в $daily-log
```

Если в одном треде было несколько разных задач:

```text
$daily-log
Раздели исправление авторизации и доработку отчёта как разные задачи.
```

Если задача относится к подпроекту большого проекта:

```text
$daily-log
Это задача проекта web-app, parent-проект product-suite.
```

Ожидаемый результат в `~/codex-daily/YYYY-MM-DD.md`:

```markdown
# 2026-07-11

> Итого по задачам: 1ч 16м · учтено задач: 1

## web-app

### Обновить тексты на главной странице

- Обновлены тексты главного экрана.
- Исправлена мобильная верстка формы.

<!--
thread: 019f...
task: update-copy
parent: product-suite
started: 2026-07-11T14:30:00+03:00
finished: 2026-07-11T15:45:57+03:00
duration: 1ч 16м
updated: 2026-07-11T15:45:57+03:00
-->
```

Время выполнения хранится отдельно для каждой задачи. Если один тред содержит несколько разных задач, у каждого блока `###` будет свой набор `started`, `finished` и `duration`.
В каждом daily-файле строка `Итого по задачам` пересчитывается при обновлении отчёта.

## Восстановление времени в старых отчётах

Посмотреть, какие старые задачи можно обновить:

```bash
python3 ~/codex-tools/backfill_codex_daily_duration.py
```

Применить уверенные обновления:

```bash
python3 ~/codex-tools/backfill_codex_daily_duration.py --write
```

Если один старый тред содержит несколько задач без явных границ времени, backfill по умолчанию пропустит такие блоки. Для примерного заполнения можно использовать:

```bash
python3 ~/codex-tools/backfill_codex_daily_duration.py --include-ambiguous --write
```

Исторически восстановленное время может быть примерным: оно считается от начала session history до `updated` задачи.

Пересчитать строку `Итого по задачам` во всех daily-файлах без изменения `duration`:

```bash
python3 ~/codex-tools/backfill_codex_daily_duration.py --refresh-summary --write
```

## Экспорт в Obsidian

```bash
python3 ~/codex-tools/export_codex_daily_to_obsidian.py --force
```

По умолчанию:

- source: `~/codex-daily`;
- vault: `~/codex-obsidian`.

Дополнительные связи подпроектов можно хранить локально вне git:

```json
{
  "web-app": "product-suite",
  "api-service": "product-suite"
}
```

По умолчанию exporter читает этот JSON из `~/codex-tools/project-parents.json`.
Другой путь можно передать через `--project-parents`.

Экспорт также создаёт агрегированный отчёт `Reports/Project Time.md` с суммарным временем по проектам и parent/root-проектам.
В daily-страницах Obsidian тоже выводится итоговое время за день.

## Автозапуск экспорта

### macOS launchd

Создай `~/Library/LaunchAgents/com.codex.daily-obsidian-export.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.codex.daily-obsidian-export</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOUR_USER/codex-tools/export_codex_daily_to_obsidian.py</string>
  </array>
  <key>StartInterval</key>
  <integer>300</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/codex-daily-obsidian-export.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/codex-daily-obsidian-export.err</string>
</dict>
</plist>
```

Замени `YOUR_USER`, затем:

```bash
launchctl load ~/Library/LaunchAgents/com.codex.daily-obsidian-export.plist
launchctl start com.codex.daily-obsidian-export
```

### Linux systemd

Создай `~/.config/systemd/user/codex-daily-obsidian-export.service`:

```ini
[Unit]
Description=Export Codex daily logs to Obsidian

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/codex-tools/export_codex_daily_to_obsidian.py
```

Создай `~/.config/systemd/user/codex-daily-obsidian-export.timer`:

```ini
[Unit]
Description=Run Codex daily Obsidian export every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=codex-daily-obsidian-export.service

[Install]
WantedBy=timers.target
```

Включи timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codex-daily-obsidian-export.timer
```

### Windows Task Scheduler

Создай задачу PowerShell:

```powershell
$Action = New-ScheduledTaskAction `
  -Execute "python" `
  -Argument "$env:USERPROFILE\codex-tools\export_codex_daily_to_obsidian.py"

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
  -TaskName "CodexDailyObsidianExport" `
  -Action $Action `
  -Trigger $Trigger `
  -Description "Export Codex daily logs to Obsidian"
```

## Настройка путей

```bash
CODEX_SKILLS_DIR=~/.agents/skills \
CODEX_TOOLS_DIR=~/codex-tools \
./install.sh
```
