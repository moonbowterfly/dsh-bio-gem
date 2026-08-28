// dsh-bio-gem 插件入口（M1）
// 导出: tools = 语义化工具定义（gemTools）；jobs = 后台长任务管理
// TODO(接入 dsh-tools 时): 按 @deepseek-ai/dsh-tools 插件注册协议把 tools 注册进宿主，
//   gem_build 的 executor 用 jobs.startBuild() + jobStatus() 实现异步进度。
const { gemTools } = require('./tools');
const jobs = require('./jobs');

module.exports = { tools: gemTools, jobs, name: 'dsh-bio-gem' };