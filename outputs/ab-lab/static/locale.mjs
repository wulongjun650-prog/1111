// Names are supplied by the browser's built-in CLDR/Intl data, not a web API.
const regionCodes = 'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'.split(' ');
const languageCodes = 'aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu'.split(' ');
const names = {
  countries: new Intl.DisplayNames(['zh-CN'], {type:'region'}),
  languages: new Intl.DisplayNames(['zh-CN'], {type:'language'}),
};
const aliases = {
  countries: {韩国:'KR',南韩:'KR',朝鲜:'KP',中国香港:'HK',香港:'HK',中国澳门:'MO',澳门:'MO',中国台湾:'TW',台湾:'TW',英国:'GB',美国:'US'},
  languages: {韩语:'ko',韩国语:'ko',朝鲜语:'ko',中文:'zh',汉语:'zh',英语:'en',英文:'en',日语:'ja',日文:'ja',越南语:'vi',法语:'fr',德语:'de',西班牙语:'es',葡萄牙语:'pt',简体中文:'zh-cn',繁体中文:'zh-tw'},
};
const extras = ['zh-cn','zh-tw','zh-hk','en-us','en-gb','pt-br'];
export const options = {
  countries: regionCodes.map(code => ({code, name:names.countries.of(code)})),
  languages: [...languageCodes,...extras].map(code => ({code,name:names.languages.of(code)})),
};
export function countryLabel(code) {
  if (!code) return '国家未知';
  const normalized = String(code).toUpperCase();
  return regionCodes.includes(normalized) ? `${names.countries.of(normalized)} · ${normalized}` : `国家未知 · ${normalized}`;
}
export function searchOptions(kind, query) {
  const text = query.trim().toLowerCase();
  const exact = aliases[kind][query.trim()];
  return options[kind].filter(item => !text || item.code.toLowerCase().includes(text) || item.name.toLowerCase().includes(text) || item.code === exact)
    .sort((a,b) => Number(b.code === exact || b.code.toLowerCase() === text) - Number(a.code === exact || a.code.toLowerCase() === text)).slice(0,30);
}
export function normalizeSelection(kind, value) {
  const parts = Array.isArray(value) ? value : String(value || '').split(/[,，\n]+/);
  return [...new Set(parts.map(part => part.trim()).filter(Boolean).map(part => {
    const aliased = aliases[kind][part];
    const item = options[kind].find(row => row.name === part || row.code.toLowerCase() === part.toLowerCase());
    if (aliased || item) return aliased || item.code;
    if (kind === 'languages' && /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/i.test(part)) return part.toLowerCase();
    throw new Error(`${kind === 'countries' ? '国家' : '语言'}“${part}”无法识别，请从搜索选项中选择。`);
  }))];
}
