# 🖥 Автозапуск Avito Parser вместе с Windows

Есть два режима автозапуска:

| Режим | Что запускается | Когда использовать |
|---|---|---|
| **Headless** (рекомендуется) | `scripts/windows/run_headless.py` через `pythonw.exe` — парсинг начинается сразу, без окна | Постоянная работа 24/7, после перезагрузки ПК |
| **Gui** | `AvitoParser.py` — открывается окно программы, парсинг нужно запустить кнопкой «Старт» | Нужен ручной контроль настроек |

Готовый скрипт создаёт задачу в **Планировщике задач Windows** при входе
пользователя (или ярлык в автозагрузке). Права администратора обычно не
нужны.

---

## 1. Подготовьте окружение

Один раз выполните в папке проекта:

```bat
run_avito_parser.bat
```

Скрипт создаст `.venv`, установит зависимости и запустит GUI. После этого
появятся:

```
<папка проекта>\.venv\Scripts\pythonw.exe
```

Если `.venv` уже есть — пропустите этот шаг.

> Важно: автозапуск использует **pythonw.exe** (без консольного окна).
> Запуск через `python.exe` тоже работает, но будет видно чёрное окно.

---

## 2. Установите автозапуск

Откройте PowerShell в папке проекта (или `cmd`) и выполните:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
```

Либо просто дважды кликните:

```
scripts\windows\install_autostart.bat
```

По умолчанию создаётся задача **`AvitoParser-4G`**:

* триггер — вход текущего пользователя;
* задержка 30 секунд (модем и сеть успевают инициализироваться);
* запуск без окна;
* перезапуск при падении (до 10 раз, раз в минуту);
* задача не останавливается при работе от батареи.

### Полезные варианты

```powershell
# Запустить сразу после установки
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -StartNow

# Открывать GUI при входе
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Mode Gui

# Другое имя задачи и задержка 60 секунд
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -TaskName "AvitoParser-Modem" -DelaySeconds 60

# Способ «Автозагрузка» (ярлык в shell:startup) вместо Планировщика
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Method Startup
```

Проверить задачу:

```powershell
Get-ScheduledTask -TaskName "AvitoParser-4G"
Get-ScheduledTaskInfo -TaskName "AvitoParser-4G"
```

Запустить/остановить вручную:

```powershell
Start-ScheduledTask -TaskName "AvitoParser-4G"
Stop-ScheduledTask  -TaskName "AvitoParser-4G"
```

---

## 3. Где смотреть логи

| Файл | Что внутри |
|---|---|
| `logs\headless.log` | stdout/stderr headless-запуска, ошибки импорта, старт/стоп |
| `logs\app.log` | основной лог парсера (с ротацией) |

Если задача не запускается:

1. Проверьте `logs\headless.log` — там будет traceback.
2. Проверьте, что `.venv\Scripts\pythonw.exe` существует.
3. Запустите вручную из папки проекта:
   ```bat
   .venv\Scripts\python.exe scripts\windows\run_headless.py
   ```
   и посмотрите вывод.
4. В Планировщике задач откройте свойства задачи → вкладка «История»:
   код результата `0x1` обычно означает неверный путь/Python.

---

## 4. Удаление автозапуска

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1
```

С остановкой текущего процесса:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1 -StopRunning
```

Для способа «Автозагрузка»:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1 -Method Startup
```

Или удалите ярлык вручную: `Win+R` → `shell:startup` → удалите
`AvitoParser 4G.lnk`.

---

## 5. Чтобы модем не «засыпал»

Для стабильной круглосуточной работы:

1. **Электропитание USB.** Диспетчер устройств → USB-концентраторы →
   свойства → «Управление электропитанием» → снимите галочку «Разрешить
   отключение этого устройства для экономии энергии».
2. **Сетевой адаптер модема.** Диспетчер устройств → сетевые адаптеры →
   свойства адаптера ZTE → «Управление электропитанием» → снимите галочку
   экономии.
3. **Сон компьютера.** Для ПК 24/7:
   ```bat
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /change monitor-timeout-ac 10
   ```
4. **USB selective suspend** (если модем периодически пропадает):
   «Электропитание» → «Настройка схемы электропитания» → «Изменить
   дополнительные параметры» → «Параметры USB» → «Временное отключение
   USB-порта» → «Запрещено».

---

## 6. Альтернатива: служба Windows (NSSM)

Если парсер должен работать **до входа пользователя** (без автологина),
задачу Планировщика можно заменить службой через [NSSM](https://nssm.cc/):

```bat
nssm install AvitoParser4G "C:\путь\к\проекту\.venv\Scripts\python.exe" "C:\путь\к\проекту\parser_cls.py"
nssm set AvitoParser4G AppDirectory "C:\путь\к\проекту"
nssm set AvitoParser4G AppStdout "C:\путь\к\проекту\logs\service.log"
nssm set AvitoParser4G AppStderr "C:\путь\к\проекту\logs\service.log"
nssm set AvitoParser4G Start SERVICE_AUTO_START
nssm start AvitoParser4G
```

Служба запускается от `LocalSystem`; учтите, что модем и `config.toml`
должны быть доступны этой учётной записи, а GUI в режиме службы не работает.
Для большинства домашних сценариев достаточно Планировщика задач.

---

## 7. Частые вопросы

**Нужно ли вводить пароль Windows при установке задачи?**
Нет, задача создаётся для текущего пользователя с типом входа
«Интерактивный» — пароль не запрашивается. Она запускается после входа
пользователя в систему.

**Парсер запустится, если компьютер перезагрузился и никто не вошёл?**
Нет. Для этого нужен автологин Windows или служба (NSSM). Инструкция по
автологину: `Win+R` → `netplwiz` → снимите «Требовать ввод имени
пользователя и пароля».

**Можно ли держать GUI открытым, а парсинг запускать автоматически?**
В режиме `Gui` программа только открывает окно. Автоматический старт
парсинга — это режим `Headless`. GUI удобен для настройки, headless — для
постоянной работы.

**Что будет при обновлении проекта?**
Задача указывает на `pythonw.exe` и файлы проекта, поэтому после обновления
кода перезапустите задачу:
```powershell
Stop-ScheduledTask -TaskName "AvitoParser-4G"
Start-ScheduledTask -TaskName "AvitoParser-4G"
```
