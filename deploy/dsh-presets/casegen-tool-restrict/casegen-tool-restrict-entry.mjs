// 新的 import specifier：用 query-string 绕过运行中 DSH 对 casegen-tool-restrict.mjs 的 ESM 模块缓存。
export { name, inject, apply, ALLOWED_TOOL_NAMES } from './casegen-tool-restrict.mjs?rev=20261109b'
