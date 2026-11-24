# University Circular Alert Bot

Dhakar university gulor lecturer, research, officer ar administrative circular protiti ghontay check kore Telegram-e pathay.

## Ki pathabe

Protiti ghontay shudhu **2 ta message** ashe. Agher ghontar 2 ta message nije theke muche jay, tai chat-e shob shomoy latest list thake.

1. **🎓 Lecturer & Research (CSE/BBA)**: Private University ar Public University alada bhag-e.
2. **🏢 Officer · Admin · HR**: private ar public eksathe, protita post-er pashe `[Private]` / `[Public]` lekha.

- Protita bhag-e je circular-er deadline age shesh, shetai upore. 🔴 = 3 diner moddhe, 🟠 = ei shoptahe.
- Je circular aj publish hoyeche, tar pashe saradin **🟢 NEW** thake. Porer din ar thake na.
- Deadline par hole circular list theke chole jay.
- Deadline lekha nai emon notun post ekta collapsed list-e 7 din thake (tap korle khule).
- Notun kichu ashle message sound kore ashe, na ashle silent.
- Kono site 6 ghonta kaj na korle Officer message-er niche dekhabe.
- Robibar sokal 9 tar por Officer message-er niche manual checklist thake.
- **Kono ghontay message na ele bot bondho ache dhore niben.**

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
