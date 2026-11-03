/**
 * 数字区间里的波浪号：`17~19℃`、`20～30 元`。
 *
 * GFM 把**成对**的 `~` 解析成删除线，所以一段话里出现两个区间时，
 * 中间那一整句会被划掉（"17~19℃…20~22℃" → 中段被删）。渲染前统一
 * 换成中文「至」，模型偶尔漏改也兜得住。
 */
const NUMERIC_RANGE_TILDE_RE = /(?<=\d)\s*[~～](?!~)\s*(?=[-−]?\d)/g;

/** 把 Markdown 里的数字区间波浪号替换成「至」，其余内容原样返回。 */
export function normalizeRangeTildes(text: string): string {
  return text.replace(NUMERIC_RANGE_TILDE_RE, "至");
}
