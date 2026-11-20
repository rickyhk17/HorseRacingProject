# 香港賽馬預測紀錄

私人使用的香港賽事資料、人工核對、預測和投注紀錄工具。預測和模擬不保證盈利；系統不會下注，也不連接投注帳戶。

## 資料來源及限制

本項目沒有已確認授權及穩定的馬會或馬評網站資料介面。程式提供受限制的公開頁面採集嘗試，但來源的使用條款及授權必須由使用者先行核實；robots.txt 允許存取並不等於授權。採集內容全屬待核對資料，不會自動更改已核實賽事、推介或賽果。外部資料可能缺漏、過時或有誤，仍須與馬會官方資料人工核對。

Threads 的已索引 robots.txt 載有未獲明確書面許可不得自動收集的限制；本系統不繞過此限制，也沒有 Threads API 存取權，現階段仍需人工輸入及核對貼文、作者和多於 1,000 位追蹤者。取得適當的授權及 API 權限後才可考慮自動化。

三重彩模擬為四匹馬 24 注，每注 $5；單T對照為四組每注 $10，另有同預算每注 $30 的對照。輸入派彩時必須記錄官方派彩的基準注額。遇到退出、平頭、退款或其他特殊情況時應以官方規則人工核實，不可假設一般計算適用。

## 本地安裝

需要 Python 3.10 或以上。以下命令在本項目目錄執行：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DJANGO_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"
export DJANGO_DEBUG=1
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

使用瀏覽器進入 `http://127.0.0.1:8000/`，以管理員登入。`DJANGO_SECRET_KEY` 必須使用私密、持久、足夠隨機的值；上述命令只產生一次，應安全保存並在每次啟動網頁及排程時使用同一值。未設定時會產生臨時密鑰，重啟後現有登入會失效。不要把密鑰、資料庫或 Telegram 憑證提交至版本庫。

以上是本機開發方式，不適合公開部署。私人主機上應關閉 debug、設定 `DJANGO_ALLOWED_HOSTS`（逗號分隔）、HTTPS 與 `DJANGO_SECURE_COOKIES=1`，使用正式應用伺服器及靜態檔案服務，限制登入存取並定期備份 SQLite 資料庫。切勿在沒有 HTTPS 的公開網絡使用管理後台。

## 運作前提

時間一律按香港時間顯示。系統只作輔助，需在發布前人工核實賽事、出賽名單、來源及預測；場次不合資格時不必湊足三場。場地、馬匹、開跑時間和賽果可能變更，應在使用前重新核對。回測和模擬應與真實投注分開；歷史回測只可使用當時已知的資料，不可由後來資料冒充歷史預測。

在管理後台先輸入香港賽日、場次及馬匹並核實，再輸入來源貼文與其對應馬匹，記錄核實人員及資料。查看儀表板候選排序後，在場次填寫選場及選馬理由，然後在同場馬匹列表選取四匹並使用批准預測操作。預測資料快照不能覆寫；修訂應另行批准。每個賽日最多五場合資格預測，已批准場次目前不能撤銷，故批准前應先核實。實際下注只作人工紀錄，與模擬分開。賽後在賽果表格新增版本並核實派彩及基準注額；退款或特殊情況應審慎處理，待人工核實時不要當作普通已結算賽事。

儀表板以每場首個實際發布的版本及最新賽果顯示模擬，沒有足夠紀錄時不代表任何策略有效。這不是完整歷史回測系統，也沒有自動收集往績、模型校準或驗證收益的能力。

## 嘗試自動採集

在使用者已核實各來源允許所需自動存取、使用及儲存資料後，可指定來源執行以下命令；`--terms-reviewed` 表示你已完成該來源的權限及條款核實，不是程式授權：

```sh
python manage.py collect_sources --source hkjc-racecard --terms-reviewed
python manage.py collect_sources --source oncc --terms-reviewed
python manage.py collect_sources --source horseracinghk --terms-reviewed
python manage.py collect_sources --source hkjc-results --due-results --terms-reviewed
```

`--due-results` 賽果輪詢只處理已有預測或實際投注、且在最新賽果紀錄中已人工填寫實際完賽時間至少 30 分鐘的本地場次；`--race-id` 同樣要求記錄完賽時間。手動指定 `--race-date`、`--venue`（`ST`／`HV`）和 `--race-number`，或指定 `--url` 查詢單頁時則不檢查完賽時間，應由操作者確認。使用 `--help` 查看完整選項。可在持續運行的私人主機以定時排程執行；勿讓多個採集命令重疊執行，並應檢查退出狀態及後台的採集紀錄。

採集先檢查目標網站的 robots.txt，拒絕不允許、無法讀取、重新導向或網絡設定不受支援的請求；不登入、不繞過網站限制。馬會頁面只保存有限表格內容供人閱讀，馬評首頁只保存有限標題及連結；不代表已自動識別馬匹、推介或正式賽果。後台 `collection` 的 Observation 顯示結果或失敗代碼，需人工核對後另行填入相關賽事、推介及賽果表格；採集紀錄本身不可直接結算。頁面格式、動態載入或截斷可能導致資料缺失。

**目前環境無法直接連接上述網站，故未能以即時網站驗證採集格式或授權。** 如果網站拒絕連接、頁面需登入、沒有授權或資料不足，請停用該來源並使用人工方式；不要繞過存取限制。

## Telegram 通知

使用者需自行建立 Telegram Bot 及取得目標對話的 chat ID。複製空白範本至本機私密設定檔並填寫：

```sh
cp notifications/telegram.credentials.example notifications/telegram.credentials
chmod 600 notifications/telegram.credentials
# 編輯 notifications/telegram.credentials，填入 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID
python manage.py run_racing_and_notifications
```

真正的設定檔已加入 `.gitignore`，不可提交或分享；範本必須保持空白。已有的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 環境變數優先於檔案。如有已安全部署的 HTTPS 網頁，可設定 `RACING_DASHBOARD_URL` 以在通知中加入連結。上述整合命令依次排隊賽事通知及處理 Telegram 發送；它執行一次便結束，並非持續服務，也不會自動建立 Bot。

可使用主機排程器每分鐘執行該命令，並確保同一組資料庫、工作目錄及環境變數。SQLite 不提供行級鎖定，應避免同時運行多個賽事排程或並行批准操作。排程器不在本程式內，需自行設定和監察；排程間隔及處理延遲會影響實際發布時間。賽事工作預設只處理開跑前後 24 小時內的相關場次；可用 `--help` 查看時間範圍和個別場次選項。

管理頁面的通知及通知處理紀錄會顯示失敗；賽事排程命令本身的錯誤須由主機排程器記錄和監察。請監察未執行、長期運行、失敗或未確定的工作。傳送逾時或中斷後，Telegram 可能已收到訊息，系統會標記「未確定」，不會自動重發；請人工核對，避免重複通知。通知內容會儲存在資料庫，不應放入密鑰或敏感資料。

賽事通知排程只處理已輸入的資料；如另行排程上面的賽果採集命令，採集結果仍需人工確認。必須人工記錄實際完賽時間，系統才可在 30 分鐘後提醒核對未確認的賽果及輪詢賽果；主機關閉、排程未運行或網絡中斷時，不能保證準時通知。
