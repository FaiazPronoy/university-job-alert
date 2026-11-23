# University Circular Alert Bot

Dhakar university gulor lecturer, research, officer ar administrative circular protiti ghontay check kore Telegram-e pathay.

## Ki pathabe

- Notun circular paile sathe sathe alert.
- Deadline-er 7, 3, 1 din age ar shesh dine reminder, sokal 9 tar daily update-e.
- Daily update, jate bojha jay bot cholche. **Kono din ei message na ele bot bondho ache dhore niben.**
- Kono site 6 ghonta kaj na korle alert.
- Protiti Robibar je university gulo bot porte pare na, tader manual checklist.

## Setup

### 1. Telegram bot banan

1. Telegram-e `@BotFather` khulun, `/newbot` pathan, naam din.
2. Je token dibe seta copy korun. Token karo sathe share korben na, code-eo rakhben na.
3. Apnar notun bot khule `/start` pathan.

### 2. Chat ID ber korun

Ei folder-e PowerShell khule:

```powershell
pip install -r requirements.txt
```

```powershell
$env:TELEGRAM_BOT_TOKEN = Read-Host "Bot token"
```

```powershell
python watcher.py --find-chat-id
```

### 3. GitHub-e tulun

1. GitHub-e ekta **public** repository banan, jemon `university-job-alert`. Public repo-te standard runner free.
2. Ei folder-er file gulo oi repository-te push korun.
3. Repository > Settings > Secrets and variables > Actions-e duita secret add korun:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

Public repo-te code ar bot-er memory (`state/state.json`) sobai dekhte parbe. Token secret-e thakle seta dekha jabe na.

### 4. Chalu korun

1. Repository-r **Actions** tab-e workflow enable korun.
2. **University circular watch > Run workflow** e `test-message` diye run korun. Telegram-e test message asbe.
3. Abar `watch` diye run korun. Prothom run-e ekhon open circular gulor ekta summary asbe.

Erpor bot protiti ghontay nijei cholbe. GitHub schedule majhe majhe kichuta deri korte pare.

## Limitation

- Bot-block kora site bypass kora hoy na. Oigulo weekly checklist-e ase.
- Shudhu newspaper ba Facebook-e deya circular dhora nao porte pare.
- Scanned PDF-er deadline automatically pora jay na. Tokhon alert-e "circular dekhun" lekha thakbe.

## File

- `watcher.py`: main bot
- `sources.final.json`: je page gulo monitor hoy
- `career_sources.csv`: shob university-r link ar status
- `test_watcher.py`: automated test

Test chalate:

```powershell
python -m unittest test_watcher
```
