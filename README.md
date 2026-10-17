# A 股 14:30 盘中候选扫描

每个交易日北京时间约 14:30 扫描一次沪深主板、创业板、北交所 A 股，排除科创板。结果写入 [REPORT.md](REPORT.md)，在手机 GitHub App 或浏览器中可直接阅读；全量及筛选结果同时保存为 CSV / JSON。[手机网页](https://sunyj040109-lab.github.io/a-share-scanner/) 从 `main/docs` 发布，首次扫描前显示占位说明。本版是盘中量价初筛，尚未实现用户战法中的历史涨停、爆量锚点和主线板块判断。

## 部署

1. 把本目录文件放在你有写入权限的 GitHub 仓库**根目录**，并推送到默认分支。
2. 在仓库的 **Actions** 页面允许工作流运行。工作流名称为 **A-share 14:30 intraday scan**；手动触发也只会在北京时间 14:30–14:45 窗口内执行。GitHub 的 `schedule` 只会从默认分支运行。
3. 此仓库的 **Settings → Pages → Build and deployment** 已设为从 `main` 分支的 `/docs` 发布。工作流提交新报告后，GitHub Pages 会自动重建网页。
4. 如果仓库策略禁止 `GITHUB_TOKEN` 写入或默认分支受保护，需要允许此工作流写入内容；否则报告不会写入仓库或网页。

## 定时与交易日

GitHub cron `17 6 * * 1-5` 是 **UTC 周一至周五 06:17**，即北京时间 14:17。提前启动用于安装依赖和运行自检；扫描脚本会等到北京时间 14:30 再取数。ChatGPT 的聊天日程计划在北京时间 14:34 开始读取仓库报告，争取 14:40 前送达分析；它和 GitHub Actions 都不提供分钟级送达保证。GitHub 可能延迟甚至漏掉定时任务，因此这个方案无法保证精确到 14:30。脚本只允许北京时间 **14:30–14:45** 开始并完成抓取；超时则跳过发布，不把晚到的行情伪装为 14:30 信号。脚本用 `exchange-calendars` 的上海交易所日历判断节假日。日历不覆盖目标日期时会报错。公共仓库长期无活动时，GitHub 也可能停用计划任务，需在 Actions 中重新启用。

## 筛选与输出

当前初筛条件在 [config.json](config.json)：截至抓取时的涨跌幅 ≥ 3%、换手率 ≥ 2%、成交额 ≥ 1 亿元。这些是工程默认值，并非用户战法的完整定义。修改筛选逻辑可编辑 `scanner.py` 的 `select()`。ST、停牌和新股仍保留在符合代码范围的全量表中；筛选所需数值缺失时不会进入命中表。

| 文件 | 用途 |
|---|---|
| `REPORT.md` | 手机阅读的 Markdown 报告 |
| `docs/index.html` | 可选 GitHub Pages 网页 |
| `docs/data/universe.csv`、`.json` | 全量股票池 |
| `docs/data/selected.csv`、`.json` | 符合条件的股票 |
| `docs/data/summary.json` | 抓取时间、市场数量、缺失值数量与筛选参数 |

CSV 使用 UTF-8 with BOM，便于 Excel 正确显示中文。JSON 中缺失数值为 `null`。本项目不连接同花顺账户，不读取你的持仓，也不提供交易指令。

## 本地运行

需要 Python 3.12+，在项目根目录执行：

```bash
python -m pip install -r requirements.txt
python scanner.py
python -m unittest discover -s tests -v
```

非交易日或北京时间 14:30–14:45 之外会跳过。首次真实行情验证需要在一个交易日的该窗口内进行。

## 数据限制

行情来自 [AKShare 的沪、深、京 A 股接口](https://akshare.akfamily.xyz/data/stock/stock.html)，底层为东方财富公开行情。免费接口可能延迟、限流、改字段或短暂不可用；接口未提供每只股票的独立报价时间，抓取完成时间不等于每条报价的真实时间。抓取有三次退避重试、必要字段校验和各市场最低条数门槛，任一市场失败则整次运行失败，不发布部分结果。最低条数门槛用于发现明显截断，不能证明交易所级别的完整性。数据仅用于研究参考，不构成买入指令或投资建议。



