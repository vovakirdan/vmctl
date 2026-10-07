# vmctl

Управление VM на Proxmox VE 9 с рабочего компьютера под Windows, Linux или macOS.
Python 3.12+, uv, Typer и терминальный интерфейс на Textual. Исполнитель
`vmctl-worker` работает только в Linux и выполняет каждый запрос на хосте
Proxmox через OpenSSH. Без базы данных, демона и HTTP API.

Proxmox — источник истины для VM. `/etc/dnsmasq.d/vmctl-hosts.conf` — источник
истины для DHCP-резервирований vmctl. Утилита не изменяет шаблоны, firewall хоста,
NAT, WireGuard и конфигурацию публичного bridge.

## Быстрый старт (Linux / WSL)

Установите одинаковую версию **0.3.0** клиента и серверного исполнителя.
Если исполнитель ещё не установлен, сначала выполните [установку на сервере](#установка-исполнителя-на-хосте-proxmox)
и проверьте [требования к хосту](#требования-к-хосту).

1. Установите клиент из каталога проекта на рабочем компьютере:

   ```sh
   git clone https://github.com/vovakirdan/vmctl.git
   cd vmctl
   uv tool install . --python 3.12
   uv tool update-shell
   vmctl config init
   ```

2. В `~/.config/vmctl/client.toml` задайте `connection.host = "pxmx"` — ваше
   существующее имя подключения SSH — и `client.ssh_key` — **публичный** ключ
   для входа в VM. Встроенное значение `pve` — редактируемый пример, оно
   не зашито в код. У WSL собственные настройки клиента и OpenSSH,
   независимые от настроек Windows.

3. Проверьте SSH и посмотрите серверную конфигурацию:

   ```sh
   ssh pxmx true
   vmctl config validate
   vmctl templates
   vmctl presets
   vmctl bootstrap --template ubuntu-server
   ```

4. Включите дополнение по Tab в Bash внутри WSL:

   ```sh
   vmctl --install-completion
   source ~/.bash_completions/vmctl.sh
   ```

5. Проверьте план и список без создания VM:

   ```sh
   vmctl create testvm ubuntu-server small --dry-run
   vmctl list
   ```

6. Посмотрите VM и попробуйте форму создания без изменений на сервере:

   ```sh
   vmctl tui --read-only
   ```

   Когда будете готовы создавать VM и управлять ими, запустите `vmctl tui`.
   Оба режима используют тот же `client.toml` и подключение SSH, что и CLI;
   дополнительный сервис не нужен.

Обычный `create` запускает VM по умолчанию. Для подключения используйте SSH-команду
из результата. Desktop-шаблоны запрашивают пароль со скрытым вводом и подтверждением
и показывают адрес RDP, не выводя пароль. Dry-run проверяет реальные требования
хоста; при ошибке подключения конфигурации dnsmasq или типа содержимого snippets
см. [устранение ошибок хранилища и dnsmasq](#устранение-ошибок-хранилища-и-dnsmasq).

## Установка клиента на рабочем компьютере

Выполните из каталога проекта на своём компьютере:

```sh
uv tool install . --python 3.12
uv tool update-shell
vmctl --help
vmctl config init
```

`vmctl config init` создаёт **только `client.toml`**, атомарно и без перезаписи
существующего файла. Права администратора на локальном компьютере не нужны;
команда не обращается к серверу. Каталог по умолчанию:

| ОС клиента | Каталог |
| --- | --- |
| Linux | `$XDG_CONFIG_HOME/vmctl`, обычно `~/.config/vmctl` |
| macOS | `~/Library/Application Support/vmctl` |
| Windows | `%APPDATA%\vmctl` |

`--config-dir PATH` задаёт другой каталог клиента. Команда выводит фактический
путь; отредактируйте находящийся там `client.toml`. Встроенный пример:
[`config/client.toml`](config/client.toml).

```toml
[connection]
host = "pxmx"
worker = "/opt/vmctl/bin/vmctl-worker"
config_dir = "/etc/vmctl"
sudo = false
connect_timeout = 10
operation_timeout = 3600

[client]
ssh_key = "~/.ssh/id_ed25519.pub"
completion_timeout = 3
```

`connection.host` — имя подключения OpenSSH. Аутентификацию, файлы ключей, порт
и ProxyJump настройте в OpenSSH на рабочем компьютере (`~/.ssh/config`):

```sshconfig
Host pxmx
    HostName 10.200.0.1
    User root
    IdentityFile ~/.ssh/pve_ed25519
    IdentitiesOnly yes
```

Публичный ключ для VM в `client.ssh_key` независим от ключа SSH-подключения
к серверу. `--ssh-key PATH` также указывает файл **на вашем компьютере**.
Клиент проверяет ключ и передаёт его содержимое; путь рабочего компьютера
не интерпретируется на сервере. Относительные пути публичных ключей разрешаются
относительно каталога конфигурации клиента. Приватные ключи vmctl не передаёт.

Установите OpenSSH и убедитесь, что `ssh` доступен через PATH. Подключите уже
настроенный маршрут WireGuard к `10.200.0.1`, затем один раз откройте обычное
интерактивное SSH-подключение, чтобы проверить отпечаток ключа хоста и записать
его в known_hosts. При необходимости разблокируйте защищённый паролем ключ
в локальном SSH agent:

```sh
ssh pxmx true
```

Далее vmctl использует BatchMode, строгую проверку ключа хоста, подключение
без терминала и без пересылки SSH agent. Конфигурация SSH, WireGuard и firewall
не изменяется.

## Установка исполнителя на хосте Proxmox

Выполните команды **на хосте Proxmox от root**, из каталога проекта той же
версии, с уже установленным uv:

```sh
UV_TOOL_DIR=/opt/vmctl/tools UV_TOOL_BIN_DIR=/opt/vmctl/bin uv tool install . --python 3.12
/opt/vmctl/bin/vmctl-worker --help
/opt/vmctl/bin/vmctl --local config init
/opt/vmctl/bin/vmctl --local config validate
```

Каталог `/opt/vmctl` и серверная конфигурация должны принадлежать администратору.
Абсолютный путь исполнителя позволяет не зависеть от PATH login shell.
Вместо `.` команде `uv tool install` можно передать готовый wheel. Установите
совместимые версии с обеих сторон; текущая версия **0.3.0**, версия протокола **1**.

`vmctl --local config init` устанавливает серверные TOML-файлы, системные
возможности, модули разработки и гостевые скрипты в `/etc/vmctl`. `client.toml`
туда не устанавливается. Отсутствующие файлы создаются атомарно, существующие
сохраняются, в том числе при обновлении и установке из wheel. Для другого
серверного каталога: `vmctl --local --config-dir PATH config init`.
Административная конфигурация и гостевые скрипты считаются доверенными данными;
скрипты выполняются от root внутри гостевой ОС, на хосте они не запускаются.

Для непривилегированного SSH-пользователя задайте `connection.sudo = true`
и настройте на сервере sudo без пароля для исполнителя, принадлежащего
администратору. Клиент запускает
`sudo -n -- /opt/vmctl/bin/vmctl-worker --config-dir /etc/vmctl`; пароль sudo
не запрашивается и не передаётся. Исполнителю нужны права для Proxmox,
конфигурации dnsmasq, перезапуска сервисов и общей блокировки.

После проверки серверных настроек выполните на рабочем компьютере команды,
которые не изменяют VM:

```sh
vmctl config validate
vmctl templates
vmctl presets
vmctl list
vmctl create work ubuntu-desktop normal --dry-run
```

Шаблоны, пресеты и определения bootstrap перечитываются **на сервере** при
каждом запросе. `config validate` проверяет локальные настройки, совместимость
протокола и серверные определения. Клиент не хранит копии серверной
конфигурации ресурсов или bootstrap.

## Прямой запуск на хосте и переход

Теперь SSH используется по умолчанию. Для прежних команд на хосте добавьте
`--local`: этот режим использует те же сервисы и существующую серверную
конфигурацию.

```sh
vmctl --local --config-dir /etc/vmctl presets
vmctl --local create api-test ubuntu-server small --dry-run
```

Прямой режим требует Linux и необходимых прав на хосте. В нём `--config-dir`
по умолчанию равен `/etc/vmctl`, а публичный ключ по умолчанию берётся из
серверного `config.toml`. При выводе справки и инициализации конфигурации клиента
рабочий компьютер не импортирует серверные адаптеры.

Для разработки из каталога проекта:

```sh
uv sync --locked
uv run vmctl --help
uv run vmctl --local --config-dir ./config config validate
uv run vmctl --local --config-dir ./config templates
uv run vmctl --local --config-dir ./config presets
```

`uv sync --locked` использует зафиксированные зависимости. Установка через
uv tool разрешает зависимости отдельно от `uv.lock`.

## Требования к хосту

Файлы примеров описывают следующую сеть:

| Параметр | Значение |
| --- | --- |
| WireGuard для управления | `wg0`, `10.200.0.1/24` |
| Приватный bridge для VM | `vmbr1`, `10.210.0.1/24` |
| Публичный bridge | `vmbr0`, без изменений |
| NAT | Уже настроен на хосте |
| Пул резервирования | От `10.210.0.100` до `10.210.0.199` |
| Машины, настроенные вручную | `.20` / `.21`, вне пула |
| Пользователь cloud-init | `vmadmin` |
| Публичный ключ по умолчанию в прямом режиме | `/root/.ssh/id_ed25519.pub` |
| Конфигурация dnsmasq, управляемая вручную | `/etc/dnsmasq.d/proxmox-vm.conf`, без изменений |
| Генерируемые резервирования | `/etc/dnsmasq.d/vmctl-hosts.conf` |
| Файл аренд DHCP | `/var/lib/misc/dnsmasq.leases`, только чтение |

На хосте нужны команды `qm`, `pvesh`, `pvesm`, `systemctl`, `dnsmasq`, `ip`
и `arping` из iputils для проверки конфликтов адресов. В Debian нужная версия
`arping` поставляется в пакете `iputils-arping`; у других реализаций могут
отличаться значения кодов завершения при использовании `-D`.
Указанный в конфигурации сервис dnsmasq должен быть запущен.

Конфигурация dnsmasq должна подключать генерируемый файл, например:

На Debian/Proxmox установите пакет для ARP-проверки, если он отсутствует:

```sh
apt-get install --no-install-recommends iputils-arping
```



```ini
# In the existing /etc/dnsmasq.conf, if an equivalent include is not already present:
conf-dir=/etc/dnsmasq.d,*.conf
```

vmctl проверяет наличие этого подключения и не добавляет его автоматически.
Если служба подключает каталоги через параметры запуска, укажите те же
спецификации в `network.dnsmasq_conf_dirs`; см. раздел устранения ошибок.
`dnsmasq_config` вместе с дополнительными каталогами должен описывать
конфигурацию, которую загружает действующая служба.

Изменение подключённых `.conf` файлов требует перезапуска: SIGHUP не перечитывает
основную конфигурацию. При каждом обновлении утилита проверяет новый файл,
атомарно заменяет действующий, проверяет полную конфигурацию, перезапускает
сервис и проверяет, что он работает. Во время перезапуска DNS/DHCP кратковременно
недоступен. [Руководство dnsmasq](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html).

Шаблоны должны уже существовать как локальные QEMU templates, иметь диск с ОС
и cloud-init drive, а также загружать образ с cloud-init и SSH. vmctl не создаёт
шаблоны. Шаблоны с произвольными аргументами QEMU, hook scripts или PCI passthrough
отклоняются. Указанный шаблон Ubuntu Desktop также должен соответствовать
этим требованиям cloud-init и не содержать общий многократно используемый
пароль для GUI/RDP.

`local` должен быть доступным хранилищем типа directory с включённым типом
содержимого `snippets`. При необходимости выберите другое хранилище этого типа
через `proxmox.snippets_storage`. vmctl проверяет хранилище и не изменяет
его конфигурацию. Пользовательские файлы cloud-init требуют хранилища
с поддержкой snippets.
[Документация Proxmox по Cloud-Init](https://pve.proxmox.com/wiki/Cloud-Init_Support).

## Дополнение по Tab

Typer поддерживает дополнение в Bash, Zsh, Fish и PowerShell. Для Bash/WSL нужен
**Bash 4.4+**; встроенный Bash 3.2 на macOS слишком старый — используйте штатный
Zsh или новую версию Bash. В Bash команда
`vmctl --install-completion` сохраняет скрипт дополнения и добавляет его подключение
в `~/.bashrc`. Откройте новый shell или подключите скрипт, как в быстром старте.
Запускайте установку в том shell, где используете vmctl. Для Zsh/Fish после
установки откройте новую сессию. Посмотреть скрипт без установки:

```sh
vmctl --show-completion
```

Для PowerShell проверьте вывод `--show-completion` и подключите скрипт через
свой профиль с учётом действующей политики исполнения. Встроенный установщик
Typer для PowerShell изменяет политику текущего пользователя; если это
нежелательно, используйте ручное подключение.

Примеры (`<Tab>` означает нажатие клавиши, а не ввод текста):

```text
vmctl create testvm ubu<Tab>                  # ubuntu-desktop / ubuntu-server
vmctl create testvm ubuntu-server sm<Tab>     # small
vmctl create testvm ubuntu-server small --wi<Tab>
vmctl create testvm ubuntu-server small --with rust,no<Tab>  # rust,node
vmctl create work ubuntu-desktop normal --without-system des<Tab>
vmctl info wo<Tab>                           # existing VM names
vmctl delete 10<Tab>                         # existing VMIDs
vmctl bootstrap --template ubu<Tab>
```

Если настроены оба Ubuntu-шаблона, Bash дополнит `ubu` до общего префикса
`ubuntu-`; следующее нажатие Tab покажет варианты согласно настройкам shell.
Имена команд и флаги дополняются локально. Шаблоны, пресеты, модули/профили разработки
и системные возможности берутся из редактируемых TOML-файлов сервера; имена VM
и VMID — из Proxmox. `--cpu`, `--memory`, `--disk` предлагают значения настроенных
пресетов. `--ip` предлагает `auto` и адреса пула, **не гарантируя их доступность**:
она проверяется при создании. Пути вроде `--ssh-key` использует обычное дополнение
файлов в shell. Имя новой VM и описание остаются произвольным текстом.

Динамические SSH-подсказки имеют таймаут три секунды, настраиваемый через
`client.completion_timeout` (1–30 секунд). Если конфигурация отсутствует или сервер
недоступен, динамические подсказки и ошибки не выводятся; дополнение команд,
флагов и файлов продолжает работать. Дополнение не создаёт VM и резервирования,
не запрашивает пароль и не запускает гостевые скрипты. Постоянный кеш не хранится.
Если разобранный контекст команды содержит выбранный шаблон, подсказки модулей
и возможностей учитывают совместимость; при создании она проверяется всегда.

## Обновление установленной версии

Установка через `uv tool install .` не подхватывает последующие изменения исходников.
После редактирования или обновления проекта переустановите клиент и исполнитель:

```sh
# On your workstation, from the updated project directory:
uv sync --locked
uv build --wheel
uv tool install . --python 3.12 --force --reinstall
vmctl --install-completion

# On Proxmox, from the same updated project directory:
UV_TOOL_DIR=/opt/vmctl/tools UV_TOOL_BIN_DIR=/opt/vmctl/bin uv tool install . --python 3.12 --force --reinstall
/opt/vmctl/bin/vmctl --local config init
/opt/vmctl/bin/vmctl --local config validate
```

Вместо `.` с обеих сторон можно передать wheel из `dist/`. Переустановка и
`config init` сохраняют существующие настройки; новые поля добавляются явно.
Версия 0.3.0 добавляет данные каталога для TUI и запросы управления состоянием VM,
поэтому обновите не только клиент, но и сервер. После обновления дополнения
откройте новый shell.

Если каталог проекта есть только на рабочем компьютере, передайте wheel
на сервер и переустановите исполнитель через существующее имя подключения SSH.
Команды ниже предполагают доступ от root через `pxmx` и uv по пути
`/root/.local/bin/uv`; при необходимости укажите другой путь к uv:

```sh
scp dist/vmctl-0.3.0-py3-none-any.whl pxmx:/tmp/
ssh pxmx 'UV_TOOL_DIR=/opt/vmctl/tools UV_TOOL_BIN_DIR=/opt/vmctl/bin /root/.local/bin/uv tool install /tmp/vmctl-0.3.0-py3-none-any.whl --python 3.12 --force --reinstall'
ssh pxmx '/opt/vmctl/bin/vmctl --local config init'
vmctl config validate
vmctl tui --read-only
```

Развёртывание остаётся ручным. CI выполняет проверки и собирает пакеты,
но не устанавливает новые версии на Proxmox в вашей приватной сети.

## Терминальный интерфейс

Из обновлённого каталога проекта на рабочем компьютере:

```sh
uv sync --locked
uv run vmctl tui --read-only
```

В режиме только чтения можно смотреть список и детали VM, открыть форму
создания и проверить её план. Кнопки Create, Start, Shutdown, Reboot и Delete
отключены. Проверка плана может отправлять те же ARP-запросы поиска конфликтов,
что и CLI dry-run, но не клонирует VM и не меняет настройки VM, резервирования
или сервисы. Закройте приложение и включите управление, когда будете готовы:

```sh
uv run vmctl tui
```

Установленный tool запускается так же: `vmctl tui`. Для другого каталога
клиентской конфигурации укажите глобальный `--config-dir PATH` перед `tui`.
Заголовок показывает выбранный SSH-сервер. Таблица содержит VMID, имя,
состояние, шаблон, CPU, память и управляемый IP; поиск фильтрует строки.
В деталях доступны системные возможности, модули разработки, SSH и сведения
о desktop RDP. Refresh также заново загружает определения шаблонов, пресетов
и bootstrap с SSH-worker. При прямом запуске с `--local` после редактирования
конфигурации откройте TUI заново. Start, Shutdown, Reboot и Delete работают с выбранной VM
после подтверждения конкретного действия. Shutdown запрашивает корректное
выключение гостя без принудительной остановки при неудаче.

Единая форма создания делит настройки по смыслу:

| Группа | Настройки и поведение |
| --- | --- |
| Основные | Имя VM, шаблон и пресет ресурсов |
| Ресурсы | Переопределения CPU, памяти и диска; диск шаблона не уменьшается |
| Системные возможности | Инфраструктура гостя, например `qemu-agent` и `desktop-rdp`; настройки шаблона выбираются автоматически |
| Модули и профили разработки | Дополнительные инструменты и готовые наборы, например `rust`, `node` или `surge-dev`; изначально ничего не выбрано |
| Дополнительные | IP, путь к локальному публичному SSH-ключу, описание, запуск после создания, ожидание SSH и пропуск пароля desktop-пользователя |

Рядом с каждым чекбоксом видно описание из серверного определения и пояснение
зависимостей или совместимости с ОС. При выборе возможности или модуля его
зависимости разрешаются без повторов; несовместимые варианты недоступны
для выбранного шаблона. Смена шаблона пересчитывает системные настройки
по умолчанию. Инструменты разработки выбираются только явно, в том числе
для desktop VM.

В дополнительных настройках **Start after creation** выбран по умолчанию;
снимите его, чтобы клон оставался выключенным до первого запуска.
**Skip desktop password** оставляет настройку входа в GUI/RDP другому механизму
аутентификации и показывает предупреждение. Таймаут ожидания SSH проверяет
приветствие сервера после запуска, а не завершение установок.

Для Ubuntu Desktop `desktop-rdp` выбирает зависимость `qemu-agent` и использует
GNOME Flashback для RDP; консоль сохраняет обычную сессию GNOME/Wayland.
После проверки плана создание desktop VM запрашивает пароль со скрытым вводом
и подтверждением. Пароль не появляется в плане, ходе операции или результате.
Дополнительная настройка пропуска пароля предназначена для гостей с другим
способом аутентификации. Публичный порт RDP и правила firewall хоста
не добавляются.

Нажмите Preview, чтобы увидеть фактические ресурсы и выбранные возможности
с зависимостями перед Create. Ход операции показывает сообщения исполнителя,
а не приблизительный процент готовности. Успешное создание не подтверждает
завершение bootstrap гостя; проверка приветствия SSH означает только,
что SSH-сервис отвечает. Если соединение оборвалось во время операции,
проверьте и обновите состояние VM перед повтором.

Сочетания клавиш: `Ctrl+N` открывает форму создания, `Ctrl+R` обновляет список,
`/` переводит фокус на поиск, `Enter` открывает детали выбранной строки таблицы,
`Q` закрывает приложение. Ввод текста в поле не вызывает команды поиска
или выхода. Кнопки также доступны для мыши.

## Команды

| Команда | Назначение |
| --- | --- |
| `create NAME TEMPLATE PRESET` | Полный клон, ресурсы, DHCP и cloud-init; запускает VM по умолчанию |
| `list` | Имена/VMID, состояние, ресурсы и управляемые IP |
| `info NAME_OR_VMID` | Конфигурация, системные возможности/модули разработки и метаданные RDP |
| `start NAME_OR_VMID` | Запустить выключенную VM |
| `shutdown NAME_OR_VMID [--yes]` | Подтвердить и запросить корректное выключение гостя; без принудительной остановки |
| `reboot NAME_OR_VMID [--yes]` | Подтвердить и запросить перезагрузку гостя |
| `delete NAME_OR_VMID` | Подтверждение, остановка, удаление VM и освобождение управляемых ресурсов |
| `tui [--read-only]` | Терминальный список VM, форма создания и действия; дополнительный режим просмотра и проверки плана |
| `templates` / `presets` | Имена шаблонов и значения ресурсов по умолчанию |
| `bootstrap [--template NAME]` | Отдельные списки системных возможностей, модулей и профилей разработки |
| `config init` / `config validate` | Создание настроек клиента / проверка клиентских и серверных определений |
| `COMMAND --help` | Подробное описание аргументов и параметров |

Основные параметры создания:

| Параметр | Значение |
| --- | --- |
| `--start` / `--no-start` | Запустить после настройки / оставить выключенной до первого запуска |
| `--with rust,node` | Установить выбранные инструменты разработки и их зависимости без повторов |
| `--with-system NAME` | Добавить системные возможности гостя независимо от инструментов разработки |
| `--without-system desktop-rdp` | Отключить автоматический desktop RDP, сохранив остальные настройки шаблона |
| `--cpu 16 --memory 48G --disk 160G` | Переопределить ресурсы пресета; диск шаблона никогда не уменьшается |
| `--ssh-key PATH` | Публичный OpenSSH-ключ на рабочем компьютере для входа в VM |
| `--ip auto` / `--ip ADDRESS` | Выделить свободное резервирование / запросить проверяемый адрес из пула |
| `--wait 120` | Дождаться SSH banner с хоста; завершение bootstrap не проверяется |
| `--no-desktop-password` | Пропустить пароль desktop при наличии другой настройки аутентификации |
| `--description TEXT` | Описание VM в Proxmox |
| `--dry-run` | Проверить реальные требования и вывести план без изменений и запроса пароля |

Системные возможности определяются настройками шаблона (`qemu-agent` и,
для встроенного desktop-шаблона, `desktop-rdp`). Модули разработки запускаются
только по явному `--with`. Подробности: `vmctl create --help`, `vmctl delete --help`.

Серверные определения можно проверить без Proxmox через
`vmctl --local --config-dir PATH config validate`, `templates` и `presets`.
На рабочем компьютере следующие команды обращаются к настроенному серверу:

```sh
vmctl config validate
vmctl templates
vmctl presets
```

Безопасный просмотр плана с рабочего компьютера:

```sh
vmctl create api-test ubuntu-server small --dry-run
vmctl create surge-dev ubuntu-server large --with surge-dev,docker --dry-run
vmctl create work ubuntu-desktop normal --dry-run
vmctl delete 104 --dry-run
vmctl start compat --dry-run
vmctl shutdown api-test --dry-run
vmctl reboot api-test --dry-run
```

Dry-run не клонирует VM, не получает и не резервирует VMID, не записывает
snippets или резервирования, не создаёт файл блокировки и не перезапускает
сервисы. Он проверяет текущее состояние хоста и может отправлять ARP-запросы
для обнаружения конфликтов. Будущие MAC и VMID пока неизвестны; доступность IP
проверяется повторно при выполнении операции. Dry-run никогда не запрашивает
пароль desktop-пользователя.

Примеры рабочих команд, **не выполнявшихся во время разработки**:

```sh
vmctl create api-test ubuntu-server small
vmctl create surge-dev ubuntu-server large --with rust,node,llvm,cmake
vmctl create surge-dev ubuntu-server large --with surge-dev,docker
vmctl create compat alpine small --no-start
vmctl create big-test debian heavy --cpu 16 --memory 48G --disk 160G \
  --ssh-key ~/.ssh/workstation.pub --description 'Private development VM'
vmctl create ssh-test ubuntu-server normal --ip 10.210.0.105 --wait 120
vmctl create work ubuntu-desktop normal
vmctl create work-no-rdp ubuntu-desktop normal --without-system desktop-rdp
vmctl create desktop-dev ubuntu-desktop large --with rust,node,docker
vmctl list
vmctl info surge-dev
vmctl start compat
vmctl shutdown surge-dev
vmctl reboot surge-dev --yes
vmctl delete surge-dev
vmctl delete 104 --yes
```

По умолчанию действуют `--start` и `--ip auto`. `--wait` проверяет приветствие
SSH-сервера; он не выполняет аутентификацию, не проверяет ключ хоста
и не подтверждает завершение bootstrap. Если время ожидания готовности истекло,
VM сохраняется, а результат явно сообщает об этом.

Start, shutdown и reboot также принимают однозначное имя или VMID. Shutdown
и reboot запрашивают подтверждение, если не указан `--yes`. Эти действия
не меняют резервирования и bootstrap гостя. Шаблоны и зарезервированные VMID
шаблонов недоступны для команд управления состоянием; VM с блокировкой также
отклоняются. Start требует выключенную VM, shutdown и reboot — запущенную.
Каждая команда принимает `--dry-run` для просмотра цели без изменения состояния.
Таймаут корректного выключения настраивается на сервере через
`[proxmox].stop_timeout` (по умолчанию 120 секунд). После ответа Proxmox VM
может ещё перезагружаться; результат не подтверждает готовность гостя.

`delete` может уничтожить **любую обычную VM на выбранном сервере**, включая созданные
вручную, после подтверждения или с `--yes`. Шаблоны и VMID 9000–9099 защищены
от удаления. Команда удаляет только резервирования, совпадающие с MAC этой VM,
и snippets, принадлежащие vmctl. Если имя неоднозначно, нужно указать VMID.
Команда list скрывает шаблоны и показывает VM, созданные вручную; неизвестные
данные о шаблоне или IP отображаются как `unknown`.

`--with` выбирает модули и профили разработки. `--with-system` добавляет системные
возможности, `--without-system` отключает их. Оба системных флага принимают список
через запятую. Если отключить зависимость, но оставить возможность, которой она
нужна, create завершится ошибкой до клонирования. Итог создания и `info` показывают
системные возможности и модули разработки отдельно.

Размер памяти без суффикса задаётся в MiB, размер диска — в GiB. `M/G`
и `MiB/GiB` обозначают двоичные единицы. Размер диска должен быть целым числом GiB.
Если диск шаблона уже больше запрошенного, его размер сохраняется и отображается
в результате; уменьшение диска не запрашивается. При наличии нескольких дисков
выбирается первый диск с данными в порядке загрузки; если выбрать диск таким
образом невозможно, задайте `disk_device = "scsi0"` в записи нужного шаблона.

## Редактируемая конфигурация и bootstrap

При каждом запуске исполнителя серверные TOML перечитываются; нет демона
для перезагрузки конфигурации и реестра модулей в Python. Все серверные пути
настраиваются. Относительные пути вычисляются от `connection.config_dir`
(либо `--config-dir` при прямом запуске на хосте).

```text
/etc/vmctl/
  config.toml              # Host, paths, network, storage, timeouts, binaries
  templates.toml           # Template VMIDs and OS information
  presets.toml             # Resource defaults
  profiles.toml            # Compositions of development modules/profiles
  bootstrap/system/*.toml  # System features and automatic selection
  bootstrap/modules/*.toml # Development modules and OS implementations
  bootstrap/scripts/*     # Optional guest-only scripts
```

Чтобы добавить пресет, добавьте таблицу TOML:

```toml
[presets.build]
cpu = 16
memory_mib = 49152
disk_gib = 160
```

Чтобы добавить модуль, создайте `bootstrap/modules/debug-tools.toml`:

```toml
name = "debug-tools"
description = "Optional debugging utilities"
dependencies = ["base"]

[implementations.debian]
packages = ["gdb", "strace"]
commands = [["printf", "%s\n", "Debug tools installed"]]

[implementations.rhel]
packages = ["gdb", "strace"]

[implementations.alpine]
packages = ["gdb", "strace"]
```

Затем выполните:

```sh
vmctl config validate
vmctl create debug-test debian small --with debug-tools --dry-run
```

Дополнительные настройки скрипта и версии в модуле:

```toml
name = "custom-tool"
dependencies = ["base"]
default_version = "1.2.3"

[implementations.debian]
packages = ["curl"]
commands = [["printf", "%s\n", "Installing {version}"]]
scripts = [{file = "custom-tool.sh", args = ["{username}", "{version}"]}]
```

Создайте указанный файл в `bootstrap/scripts/`; аргументы доступны в нём
как `$1`, `$2` и так далее. Путь скрипта не может выходить за пределы этого
каталога. Команды задаются массивами аргументов, а не выражениями shell хоста.
`{username}` и `{version}` подставляются в отдельные аргументы и имена пакетов;
содержимое скрипта копируется без изменений. Для использования возможностей
shell внутри команды нужно явно вызвать `sh -c` в гостевой ОС.

При выборе реализации сначала проверяется `distro:release`, затем `distro`,
затем `family`. Например, `ubuntu:noble` имеет приоритет над `ubuntu`,
а `ubuntu` — над семейством `debian`. Каждый модуль сначала устанавливает
пакеты, затем выполняет команды и скрипты; модули выполняются в стабильном
порядке с учётом зависимостей. Профили рекурсивно раскрываются в модули
и другие профили:

```toml
[profiles]
surge-dev = ["base", "rust", "node", "llvm", "cmake"]
debug-dev = ["surge-dev", "debug-tools"]
```

Повторяющиеся зависимости выполняются один раз; циклы и неизвестные ссылки
вызывают ошибку до клонирования. Имя модуля должно совпадать с именем файла
и не должно совпадать с именем профиля. Команда `config validate` проверяет
все определения и ссылки на скрипты. Поддержка запрошенной ОС проверяется
на предварительном этапе create. Изменения применяются к новым VM;
существующие гости и их cloud-init snippets не перезаписываются.

## Системные возможности и desktop RDP

Системные возможности имеют отдельные определения и граф зависимостей,
отдельно от модулей и профилей разработки. Каталог системных определений
по умолчанию задаётся в `config.toml`:

```toml
[bootstrap]
system_features_dir = "bootstrap/system"
```

Поставляются возможности `qemu-agent` и `desktop-rdp`. По умолчанию `qemu-agent`
применяется ко всем шаблонам и обеспечивает интеграцию гостя, CA certificates
и sudo. `desktop-rdp` применяется при `desktop = true` и зависит от
`qemu-agent`; общие зависимости выполняются один раз. Системный bootstrap
не добавляет инструменты разработки и не обновляет всю ОС.

Явный список `system_features` в TOML шаблона заменяет автоматический выбор;
пустой список означает отсутствие системных возможностей. В примерах шаблонов
выбор задан явно:

```toml
[templates.ubuntu-server]
vmid = 9000
family = "debian"
distro = "ubuntu"
release = "noble"
desktop = false
system_features = ["qemu-agent"]

[templates.ubuntu-desktop]
vmid = 9001
family = "debian"
distro = "ubuntu"
release = "noble"
desktop = true
system_features = ["qemu-agent", "desktop-rdp"]
```

Каждый `bootstrap/system/NAME.toml` задаёт имя, описание, `supported_families`,
зависимости, правило `auto_apply` и реализации для ОС. Правила автоматического
выбора: `always`, `desktop` и `never` (по умолчанию). Выбор реализации,
а также команды и скрипты внутри гостя следуют тем же правилам, что и модули
разработки выше. Зависимости остаются внутри своей категории. Новые системные
возможности можно добавлять через эти TOML-файлы, не редактируя Python.
Неподдерживаемая комбинация возможности и ОС отклоняется до клонирования.
Поставляемая реализация `desktop-rdp` поддерживает Ubuntu и Debian; для Rocky
и Alpine нужны собственные реализации.

Например, добавьте `bootstrap/system/guest-tools.toml`:

```toml
name = "guest-tools"
description = "Optional guest integration utilities"
supported_families = ["debian"]
dependencies = ["qemu-agent"]
auto_apply = "never"

[implementations.debian]
packages = ["curl"]
files = [{path = "/etc/vmctl-guest-note", content = "Managed guest\n", owner = "root:root", permissions = "0644"}]
```

Файлы создаются через cloud-init и устанавливаются атомарно. Пути, содержимое
и владелец поддерживают подстановку настроенного `{username}`. Выбирайте
системную возможность независимо от модулей разработки:

```sh
vmctl config validate
vmctl create guest-test debian small --with-system guest-tools --dry-run
```

При обновлении существующей конфигурации выполните `vmctl --local config init` на хосте, чтобы
установить новые системные определения и скрипты. Существующие файлы сохраняются.
Прежний параметр `bootstrap.system_module` оставлен для перехода со старой
конфигурации; указанный в нём модуль исключается из выбора модулей разработки.
Перенесите собственные настройки интеграции из старого
`bootstrap/modules/system.toml` в `bootstrap/system/qemu-agent.toml`, затем
удалите старый параметр и модуль после переноса.

Для desktop-шаблонов локальный клиент запрашивает пароль и подтверждение со скрытым
вводом, в том числе при `--no-start`. Это пароль гостевой учётной записи
для GUI/RDP; SSH остаётся настроенным на аутентификацию публичным ключом.
Пароль запрашивается и при отключённом RDP, поскольку локальному GUI тоже
нужна аутентификация. Для server-шаблонов и dry-run запроса нет.

```sh
vmctl create work ubuntu-desktop normal
vmctl create work-no-rdp ubuntu-desktop normal --without-system desktop-rdp
vmctl create externally-authenticated ubuntu-desktop normal --no-desktop-password
```

`--no-desktop-password` пропускает настройку и предупреждает, что вход в GUI/RDP
может быть недоступен без другого способа аутентификации. Открытый пароль
никогда не записывается в TOML, не выводится, не логируется и не передаётся
аргументом внешних команд. Cloud-init получает SHA512-crypt хэш со случайной
солью и 500000 раундов в user-data snippet с режимом `0600`. Хэш вычисляется
локально; через stdin SSH передаётся только хэш. Публичный план и ответы
исполнителя не содержат user-data и паролей; `info` скрывает данные аутентификации
cloud-init на сервере перед передачей. Поле `hashed_passwd`
обновляет пароль и для пользователя, который уже есть в шаблоне.
[Справочник cloud-init по пользователям и группам](https://docs.cloud-init.io/en/latest/reference/modules.html#users-and-groups).
Этот snippet требует защиты: он содержит хэш пароля, хотя открытый пароль
в нём не хранится.

В Ubuntu/Debian `desktop-rdp` устанавливает `xrdp`, `xorgxrdp`,
`gnome-session-flashback`, `dbus-x11`, `ssl-cert` и необходимые утилиты сессии X11.
Пакет агента устанавливает зависимость `qemu-agent`. Возможность атомарно
создаёт `/home/vmadmin/.xsession`, принадлежащий `vmadmin:vmadmin`,
с режимом `0600`:

```sh
exec gnome-session --session=gnome-flashback-metacity
```

Путь и владелец определяются параметром `username` верхнего уровня в `config.toml`,
а не фиксированным именем `vmadmin`. XRDP использует GNOME Flashback через X11,
а консоль сохраняет обычную сессию GNOME/Wayland. Systemd targets сессии GNOME
не исправляются. Сервисная учётная запись XRDP включается в группу `ssl-cert`
для чтения TLS-ключа из пакета. Bootstrap включает `xrdp`, перезапускает
`xrdp-sesman` и `xrdp`, чтобы применить членство в группе, и проверяет активность
обоих сервисов. Guest agent запускается без требования успешного
`systemctl enable` для его потенциально статического unit.

Итог успешного создания показывает адрес RDP и пользователя только для
desktop VM с выбранной возможностью `desktop-rdp`. Пароль не выводится:

```text
SSH:
  ssh vmadmin@10.210.0.106

RDP:
  10.210.0.106:3389
  user: vmadmin
```

Используйте существующую приватную сеть VM и маршрут через WireGuard для RDP.
vmctl не добавляет публичные port forwards и не изменяет firewall Proxmox.
`info` показывает признак desktop, выбранные системные возможности, модули
разработки и настроенный адрес RDP из несекретных metadata в description Proxmox.
Это сведения о запланированной настройке, а не проверка сервисов внутри гостя;
у старых VM и созданных вручную metadata может быть неизвестна. Для server VM
раздел RDP не выводится.

## Настройки bootstrap разработки по умолчанию

Development bootstrap включается явно; `legacy-test debian small` не устанавливает
Rust, Node.js, Go, Docker, LLVM и инструменты сборки.

| Системная возможность | Ubuntu / Debian | Rocky 9 | Alpine |
| --- | --- | --- | --- |
| qemu-agent | apt / systemd | dnf / systemd | apk / OpenRC |
| desktop-rdp | XRDP / GNOME Flashback | Готовая реализация не поставляется | Готовая реализация не поставляется |

| Модуль разработки | Ubuntu / Debian | Rocky 9 | Alpine |
| --- | --- | --- | --- |
| base | Инструменты сборки из дистрибутива | Инструменты сборки из дистрибутива | Инструменты сборки из дистрибутива |
| rust | rustup stable от разработчиков Rust | rustup stable от разработчиков Rust | rustup stable от разработчиков Rust |
| node | Официальный бинарный пакет LTS | Официальный бинарный пакет LTS | Готовая реализация не поставляется |
| docker | Официальный stable-репозиторий apt | Репозиторий Docker CE, совместимый с RHEL | Готовая реализация не поставляется |
| llvm, cmake | Пакеты дистрибутива | Пакеты дистрибутива | Пакеты дистрибутива |

`base` включается явно или как зависимость модуля разработки. Rust устанавливается
для `vmadmin`, Node — в `/usr/local`. Для Docker используется `sudo docker`;
пользователь cloud-init не добавляется автоматически в группу docker, которая
даёт права, эквивалентные root. Версии из каналов Rust stable и Node LTS
определяются во время bootstrap гостя, поэтому они могут различаться между
созданиями VM. Для воспроизводимости задайте точную версию Rust toolchain
или Node в `default_version` TOML-файла модуля. Поставляемые скрипты Docker
поддерживают только канал stable. Модель запросов сервисного слоя предусматривает
переопределение версии каждого модуля для будущих флагов frontend.

Бинарные пакеты Node предназначены для гостей x86_64/aarch64 с glibc.
Неподдерживаемые запросы node/docker для Alpine отклоняются до клонирования.
Для их поддержки добавьте собственные реализации для Alpine. В примере release
для Alpine задан как `unknown`; укажите реальную версию, если используете
реализации для конкретного release. Go, Python, Postgres, Redis и профиль full-dev
можно добавить через файлы модулей; в этом MVP они не поставляются.

Пользовательский user-data явно настраивает учётную запись, отличную от root,
публичные ключи, hostname, отключение парольной аутентификации SSH и bootstrap.
Proxmox предоставляет сетевые данные DHCP. Скрипты включаются в сохраняемый
snippet, поэтому создание VM не зависит от последующих изменений исходных
определений модулей.

Успешное создание VM означает завершение клонирования, настройки,
резервирования адреса и запуска. Завершение bootstrap внутри гостя
**не проверяется**. Чтобы проверить состояние запущенного гостя:

```sh
ssh vmadmin@10.210.0.105 'sudo cloud-init status --long'
```

## Устранение ошибок хранилища и dnsmasq

**«Storage 'local' must have snippets enabled».** Для custom cloud-init user-data
хранилище должно поддерживать snippets. В веб-интерфейсе Proxmox откройте
**Datacenter → Storage → local → Edit → Content**, добавьте **Snippets**, сохранив
все уже выбранные типы содержимого. Повторите `create --dry-run`. Одного создания
каталога `snippets/` недостаточно. vmctl не редактирует конфигурацию хранилищ
Proxmox. Альтернатива: в серверном `/etc/vmctl/config.toml` задайте
`[proxmox].snippets_storage` — другое активное хранилище типа directory,
поддерживающее snippets на целевом узле.
[Документация Proxmox Cloud-Init](https://github.com/proxmox/pve-docs/blob/master/qm-cloud-init.adoc).

Посмотрите настройки хранилища на хосте Proxmox:

```sh
pvesh get /storage/local --output-format json
```

Если текущий список содержимого **в точности** `iso,vztmpl,backup`, эквивалентное
административное изменение выглядит так. Если список отличается, сохраните
фактические типы и добавьте `snippets`: `--content` заменяет весь список.

```sh
pvesm set local --content iso,vztmpl,backup,snippets
```

**«dnsmasq config must include …/vmctl-hosts.conf».** Служба и проверки vmctl
должны загружать одинаковую конфигурацию. Debian обычно подключает
`/etc/dnsmasq.d` через `CONFIG_DIR` в `/etc/default/dnsmasq`, передаваемый как `-7`,
а не через `/etc/dnsmasq.conf`. Для такой настройки добавьте поле в существующую
секцию `[network]` **серверного** `/etc/vmctl/config.toml`:

```toml
[network]
dnsmasq_conf_dirs = ["/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new"]
```

Укажите каталог и фильтры суффиксов, совпадающие с параметрами действующей службы.
vmctl передаёт эти каталоги в `dnsmasq --test` и проверяет, что они включают файл
резервирований. Саму службу эта настройка не перенастраивает. Если основной
конфиг dnsmasq уже подключает каталог, оставьте список пустым. Не добавляйте
повторное подключение одного каталога одновременно в основной файл и параметры
службы. После исправления повторите
`vmctl create testvm ubuntu-server small --dry-run`. `config validate` проверяет
определения; предварительный план создания дополнительно проверяет действующие
хранилища и dnsmasq.

## Обработка ошибок и восстановление

Команды, изменяющие состояние, используют общую блокировку хоста с ограниченным
временем ожидания. Она сериализует вызовы vmctl, но не внешние изменения через
GUI/API и не динамическое выделение адресов dnsmasq. Резервирования, существующие
аренды DHCP, таблица соседей и ответы ARP позволяют исключить занятые IP.
ARP не может доказать, что статический адрес выключенной или не отвечающей машины
свободен; активный динамический DHCP также может одновременно выделить адрес
из того же пула `.100–.199`. Храните ручные резервирования для выключенных машин
вне этого пула и проверяйте сообщения о конфликтах.

Генерируемый файл имеет строгий формат и предупреждение. Неожиданное содержимое
отклоняется, а не молча перезаписывается. Временные файлы скрыты от загрузки
каталога конфигурации dnsmasq; замена использует fsync и атомарное переименование.
Утилита отказывается заменять файлы по символическим ссылкам. Если активация
новой конфигурации не удалась, предыдущий файл восстанавливается, а сервис
перезапускается с прежней конфигурацией. Ошибки восстановления явно сообщаются.

При ошибке создания удаляются только ресурсы с меткой текущего вызова,
записанной при clone. Если уничтожение VM нельзя подтвердить, резервирование
и snippet сохраняются. Ошибка или таймаут clone означают неопределённый исход;
VMID выводится для ручной проверки. vmctl не снимает блокировки задач Proxmox
и не уничтожает VM с неопределённым состоянием. Сбой процесса или питания может
оставить ресурсы: нет демона автоматического восстановления и постоянного
журнала транзакций. Комментарии в файле резервирований и metadata в description
Proxmox помогают определить ресурсы для проверки.

Если clone сообщил о неопределённом исходе, проверьте задачи **на хосте Proxmox**:

```sh
qm config 104
pvesh get /nodes/$(hostname -s)/tasks --output-format json
```

Дождитесь завершения задачи Proxmox, прежде чем принимать решение об очистке.
После снятия блокировки VM команда `vmctl delete 104 --dry-run` позволяет
посмотреть план очистки.

Delete освобождает DHCP-резервирование только после успешного уничтожения VM
и проверки её отсутствия в списке VM. Ошибка DHCP после destroy сообщается
как частично завершённое удаление; прежнее резервирование сохраняется.
Устраните ошибку dnsmasq, затем удалите устаревшее резервирование через
проверенное администратором атомарное обновление файла и выполните проверку
и перезапуск dnsmasq. После уничтожения дисков rollback не может восстановить VM.

## Протокол SSH и прерванные операции

Каждый вызов запускает отдельный исполнитель, передаёт через stdin один
JSON-запрос с версией протокола и получает JSON Lines (`hello`, `progress`,
`result` либо `error`). Допустим только фиксированный набор операций;
некорректные запросы и версии протокола отклоняются до создания серверных
сервисов. Удалённая оболочка получает только экранированный путь исполнителя,
каталог конфигурации и необязательный `sudo -n`; имена VM, списки модулей,
публичные ключи и хэши паролей не становятся аргументами оболочки.

Перед отправкой `create` выводит ID запроса. Этот ID сохраняется в метке
вызова Proxmox, в том числе при клонировании. Повтор ID, уже связанного с VM,
отклоняется под блокировкой хоста с указанием VMID. Это обнаруживает
существующий clone, но не подтверждает завершение всей настройки. После
удаления этой VM постоянной истории запросов нет.

Если SSH оборвался, клиент прерван или истёк таймаут без определённого ответа
на create/delete/start/shutdown/reboot, результат **неизвестен**. Клиент сообщает
ID запроса и известный VMID и никогда не повторяет операцию автоматически. Сервер мог
завершить работу, продолжать её или остановиться; SSH-сессия не гарантирует,
что исполнитель переживёт разрыв. Перед повтором проверьте `vmctl list`,
`vmctl info VMID` и задачи Proxmox. Новый вызов CLI получает новый ID,
поэтому после разрыва не следует повторять создание без проверки состояния.

Удаление передаёт подтверждённые VMID, имя и отпечаток конфигурации. Сервер
повторно проверяет подтверждение, затем ещё раз проверяет его под блокировкой
изменений; изменённая конфигурация требует нового просмотра перед уничтожением.

## Структура проекта

```text
vmctl/
  pyproject.toml, uv.lock
  README.md, README.ru.md
  config/
    client.toml
    config.toml, templates.toml, presets.toml, profiles.toml
    bootstrap/{system,modules,scripts}/
  src/vmctl/
    cli.py, completion.py, client_config.py, frontend.py
    tui/                    # Textual app, creation form and dialogs
    operations.py, protocol.py, ssh.py
    worker.py, local.py
    models.py, config.py, errors.py
    services/catalog.py     # Read-only frontend choices from server TOML
    {proxmox,network,bootstrap,services,utils}/
  tests/
    client/                 # Portable client and fake SSH process tests
    test_worker.py          # Protocol and host services with fake Proxmox
    test_*.py               # Existing domain, bootstrap and host tests
  .github/workflows/tests.yml
```

## Разработка и проверки

```sh
uv sync --locked
uv run pytest
uv run pytest tests/client
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Тесты используют временные пути и имитацию subprocess runner с состоянием.
Они не требуют Proxmox, не обращаются к нему и никогда не изменяют настоящий
`/etc`. Отдельные тесты subprocess запускают безопасные локальные дочерние
процессы Python для проверки передачи аргументов, скрытия секретов и таймаутов.
Синтаксис гостевых скриптов можно проверить через `sh -n` без их выполнения.

Переносимый интерфейс `Operations` реализуют `SSHOperations` и `LocalOperations`.
Сервисный слой принимает конфигурацию, адаптеры с runner и интерфейс исполнителя
bootstrap. В нём нет запросов подтверждения Typer, консольных таблиц
и форматирования для терминала. Выбор возможностей, проверка поддержки ОС
и настройка доступны через повторно используемые сервисы. Typer и Textual —
отдельные интерфейсы поверх одних типизированных операций; каждый отвечает
за отображение, подтверждения и безопасный ввод пароля. Блокирующие вызовы SSH
работают вне цикла событий TUI, поэтому интерфейс продолжает отображать
ход операции и отвечать на действия пользователя.

Локальные тесты подтверждают разбор данных и порядок операций, но не работу
на настоящем Proxmox, получение аренды DHCP, загрузку гостя и успешную установку
инструментов из внешних источников. Эти проверки выполняются отдельно
на временных VM с явным разрешением на операции.

Конфигурация CI запускает `tests/client` на настоящих Windows, Linux и macOS
с Python 3.12–3.14, а весь набор серверных тестов — в Linux. Настройка использует
[официальные примеры setup-uv](https://github.com/astral-sh/setup-uv)
и [документацию checkout](https://github.com/actions/checkout).
Наличие конфигурации не подтверждает завершённый запуск на всех ОС: текущие
локальные проверки выполнены в Linux. Приёмка настоящего SSH/Proxmox/RDP
остаётся отдельной проверкой.
