import sys
import time
import warnings

import pandas as pd
import requests
import matplotlib

# 图表后端：优先使用带 GUI 的 TkAgg（本机运行时弹窗显示图表）；
# 若当前环境无 GUI / 无 tkinter（如服务器、CI），自动回退到 Agg（不弹窗，
# 脚本仍可正常输出报告并保存 CSV）。
try:
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except Exception:
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

# 让图表里的中文正常显示（Windows 常见中文字体）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False  # 正常显示负号

# 忽略可能的版本提示类警告，保持输出干净
warnings.filterwarnings("ignore")

# ============ 版本信息 ============
__version__ = "0.1.0"            # 当前版本号（用于发布标记）
PROGRAM_NAME = "A 股量化分析工具"  # 程序名称

# 默认股票：贵州茅台（可在运行脚本时用参数覆盖，例如 python quant1.py 000001）
DEFAULT_TICKER = "600519"
DAYS = 120  # 最近 120 个交易日（约半年），足够画出有意义的均线

# 腾讯日K线接口（国内数据源，已验证可用）
TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 新浪日K线接口（备用回退数据源）
SINA_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"


def to_tencent_symbol(ticker: str) -> str:
    """把 A 股代码转成腾讯格式：6 开头→sh，0/3 开头→sz。"""
    if ticker.startswith("6"):
        return f"sh{ticker}"
    if ticker.startswith(("0", "3")):
        return f"sz{ticker}"
    raise ValueError(f"无法识别股票代码 {ticker} 所属市场（仅支持 6/0/3 开头的 A 股）")


def build_session() -> requests.Session:
    """构造带浏览器 User-Agent 的会话，请求更稳定。"""
    session = requests.Session()
    session.headers[
        "User-Agent"
    ] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    return session


def fetch_kline(ticker: str, days: int = DAYS, max_attempts: int = 3) -> pd.DataFrame:
    """下载日K线数据：优先腾讯，失败自动回退新浪，均带重试。"""
    errors = []
    for source, fetcher in (
        ("腾讯", _fetch_from_tencent),
        ("新浪", _fetch_from_sina),
    ):
        try:
            print(f"正在从{source}下载 {ticker} 日K数据 ...")
            df, name = fetcher(ticker, days, max_attempts)
            # 新浪接口不返回真实名称，回退时 name 会是 "sh600519" 形式的代码，
            # 去掉市场前缀，保持标题统一为「600519」
            if name[:2] in ("sh", "sz", "bj") and len(name) == 8:
                name = name[2:]
            print(f"下载成功：{name}（{ticker}），共 {len(df)} 个交易日（数据源：{source}）\n")
            return df, name
        except Exception as e:
            errors.append(f"{source}：{e}")
            print(f"  {source} 数据源不可用，尝试下一个 ...\n")
    raise RuntimeError(" / ".join(errors))


def _fetch_from_tencent(ticker: str, days: int, max_attempts: int):
    """腾讯证券日K线：param=代码,day,起始,条数,复权类型"""
    symbol = to_tencent_symbol(ticker)
    params = {"param": f"{symbol},day,,,{days},qfq"}
    session = build_session()
    attempt = 0
    wait = 2
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.get(TENCENT_URL, params=params, timeout=15)
            resp.raise_for_status()
            node = (resp.json().get("data") or {}).get(symbol) or {}
            lines = node.get("qfqday") or node.get("day") or []
            if not lines:
                raise ValueError(f"未获取到 {ticker} 的行情数据，请检查代码是否正确")
            # 从实时行情节点里解析股票真实名称（如"贵州茅台"）
            name = ticker
            qt = node.get("qt") or {}
            qt_info = qt.get(symbol)
            if isinstance(qt_info, list) and len(qt_info) > 1 and qt_info[1]:
                name = qt_info[1]
            return _parse_tencent(lines), name
        except Exception as e:
            if attempt >= max_attempts:
                raise
            print(f"  [腾讯] 出错（{e}），等待 {wait} 秒后重试 ...")
            time.sleep(wait)
            wait *= 2
    raise RuntimeError("腾讯下载失败")


def _fetch_from_sina(ticker: str, days: int, max_attempts: int):
    """新浪日K线：symbol=sh600519&scale=240(日线)&datalen=条数"""
    symbol = to_tencent_symbol(ticker)
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(days)}
    session = build_session()
    attempt = 0
    wait = 2
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.get(SINA_URL, params=params, timeout=15)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                raise ValueError(f"未获取到 {ticker} 的行情数据，请检查代码是否正确")
            return _parse_sina(rows), symbol
        except Exception as e:
            if attempt >= max_attempts:
                raise
            print(f"  [新浪] 出错（{e}），等待 {wait} 秒后重试 ...")
            time.sleep(wait)
            wait *= 2
    raise RuntimeError("新浪下载失败")


def _parse_tencent(lines: list) -> pd.DataFrame:
    """解析腾讯返回的K线列表。

    每行：[日期, 开盘, 收盘, 最高, 最低, 成交量(手)]
    """
    rows = []
    for parts in lines:
        rows.append({
            "Date": pd.to_datetime(parts[0]),
            "Open": float(parts[1]),
            "Close": float(parts[2]),
            "High": float(parts[3]),
            "Low": float(parts[4]),
            "Volume": float(parts[5]),   # 单位：手
        })
    return _finalize(rows)


def _parse_sina(rows: list) -> pd.DataFrame:
    """解析新浪返回的K线 JSON（字段为字符串）。"""
    parsed = []
    for r in rows:
        parsed.append({
            "Date": pd.to_datetime(r["day"]),
            "Open": float(r["open"]),
            "Close": float(r["close"]),
            "High": float(r["high"]),
            "Low": float(r["low"]),
            "Volume": float(r["volume"]),
        })
    return _finalize(parsed)


def _finalize(rows: list) -> pd.DataFrame:
    """构建 DataFrame，按日期升序排列，并补充涨跌幅。"""
    df = pd.DataFrame(rows).set_index("Date").sort_index()
    df.index.name = "Date"
    df["ChangePct"] = df["Close"].pct_change() * 100  # 当日涨跌幅(%)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """添加均线(MA5/MA20)、RSI 等常用技术指标。"""
    df = df.copy()
    df["MA5"] = df["Close"].rolling(window=5).mean()
    df["MA20"] = df["Close"].rolling(window=20).mean()

    # RSI(14)：相对强弱指标
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(window=14).mean()
    loss = (-delta.clip(upper=0)).rolling(window=14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def metrics_from_returns(daily_ret: pd.Series) -> dict:
    """根据日收益率序列计算绩效指标（复利口径）。

    供买入持有与回测策略共用的底层指标函数。
    """
    daily_ret = daily_ret.dropna()
    n = len(daily_ret)
    if n < 2:
        return None

    TRADING_DAYS = 244  # A 股每年约 244 个交易日

    # 累计收益率（复利）
    total_return = (1 + daily_ret).prod() - 1
    # 年化收益率
    ann_return = (1 + total_return) ** (TRADING_DAYS / n) - 1
    # 年化波动率
    ann_vol = daily_ret.std() * (TRADING_DAYS ** 0.5)
    # 夏普比率（无风险利率按 0 处理）
    sharpe = (daily_ret.mean() * TRADING_DAYS) / ann_vol if ann_vol > 0 else float("nan")
    # 最大回撤（基于净值曲线）
    equity = (1 + daily_ret).cumprod()
    drawdown = equity / equity.cummax() - 1
    max_drawdown = drawdown.min()
    # 胜率与盈亏比（按日涨跌统计）
    up = daily_ret[daily_ret > 0]
    down = daily_ret[daily_ret < 0]
    win_rate = len(up) / len(daily_ret) * 100
    avg_gain = up.mean() if len(up) else 0.0
    avg_loss = -down.mean() if len(down) else 0.0
    profit_loss = avg_gain / avg_loss if avg_loss > 0 else float("inf")

    return {
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "profit_loss": profit_loss,
    }


def compute_performance_metrics(df: pd.DataFrame) -> dict:
    """计算买入持有的核心量化绩效指标（复用收益序列指标函数）。"""
    close = df["Close"]
    daily_ret = close.pct_change()
    return metrics_from_returns(daily_ret)


def print_performance_report(df: pd.DataFrame, name: str = "", ticker: str = "") -> None:
    """打印核心量化绩效指标。"""
    metrics = compute_performance_metrics(df)
    if metrics is None:
        print("\n数据不足，无法计算绩效指标。")
        return

    n = len(df)
    title = f"{name}（{ticker}）" if name else ticker
    print("\n" + "=" * 52)
    print(f"  专业绩效报告 — {title}")
    print("=" * 52)
    print(f"  统计区间    {df.index[0].date()}  →  {df.index[-1].date()}（{n} 个交易日）")
    print(f"  累计收益率  {metrics['total_return'] * 100:>10.2f}%")
    print(f"  年化收益率  {metrics['ann_return'] * 100:>10.2f}%")
    print(f"  年化波动率  {metrics['ann_vol'] * 100:>10.2f}%")
    print(f"  夏普比率    {metrics['sharpe']:>10.2f}")
    print(f"  最大回撤    {metrics['max_drawdown'] * 100:>10.2f}%")
    print(f"  胜率        {metrics['win_rate']:>10.2f}%")
    print(f"  盈亏比      {metrics['profit_loss']:>10.2f}")
    print("=" * 52)


def backtest_ma_strategy(df: pd.DataFrame, short_win: int, long_win: int) -> pd.DataFrame:
    """双均线策略回测：短均线上穿长均线时持有，下穿时空仓。

    信号次日生效（shift(1)），避免使用未来数据。返回带策略收益的 DataFrame。
    """
    bt = df.copy()
    bt["MA_short"] = bt["Close"].rolling(short_win).mean()
    bt["MA_long"] = bt["Close"].rolling(long_win).mean()
    # 持仓信号：短均线 > 长均线 → 1（持有），否则 0（空仓）
    bt["signal"] = (bt["MA_short"] > bt["MA_long"]).astype(int)
    # 次日生效：今天收盘产生的信号，明天才执行
    bt["position"] = bt["signal"].shift(1).fillna(0)
    bt["market_ret"] = bt["Close"].pct_change().fillna(0)
    bt["strategy_ret"] = bt["position"] * bt["market_ret"]
    return bt


def parameter_scanner(
    df: pd.DataFrame,
    name: str = "",
    ticker: str = "",
    param_grid: list = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """策略参数扫描器：遍历多组均线参数组合，回测后按夏普比率排名。

    返回结果 DataFrame（已按夏普降序）；verbose=True 时在终端打印"体检报告"。
    """
    # 默认参数网格：短/长均线组合
    if param_grid is None:
        param_grid = [
            (5, 20), (5, 30), (5, 60),
            (10, 20), (10, 30), (10, 60),
            (20, 30), (20, 60), (20, 120),
            (30, 60), (30, 120), (60, 120),
        ]

    # 买入持有作为基准
    bh_metrics = compute_performance_metrics(df)

    rows = []
    for short_win, long_win in param_grid:
        if short_win >= long_win:
            continue
        bt = backtest_ma_strategy(df, short_win, long_win)
        m = metrics_from_returns(bt["strategy_ret"])
        if m is None:
            continue
        # 统计在市的交易日占比
        in_market = bt["position"].mean() * 100
        rows.append({
            "组合": f"MA{short_win}/MA{long_win}",
            "short": short_win,
            "long": long_win,
            "夏普": m["sharpe"],
            "年化收益%": m["ann_return"] * 100,
            "最大回撤%": m["max_drawdown"] * 100,
            "胜率%": m["win_rate"],
            "盈亏比": m["profit_loss"],
            "在市占比%": in_market,
        })

    # 过滤掉夏普为 NaN 的组合（样本过少导致），避免 NaN 排在最前面
    result = (
        pd.DataFrame(rows)
        .dropna(subset=["夏普"])
        .sort_values("夏普", ascending=False)
        .reset_index(drop=True)
    )
    result["排名"] = result.index + 1

    if not verbose:
        return result

    title = f"{name}（{ticker}）" if name else ticker
    print("\n" + "=" * 76)
    print(f"  策略参数扫描报告 — {title}（按夏普比率排名）")
    print("=" * 76)
    if bh_metrics is not None:
        print(f"  【基准·买入持有】夏普 {bh_metrics['sharpe']:.2f} | "
              f"年化 {bh_metrics['ann_return'] * 100:+.2f}% | "
              f"最大回撤 {bh_metrics['max_drawdown'] * 100:.2f}%")
        print("-" * 76)

    # 所有组合的夏普均无效（数据过少/波动为 0）时直接返回，避免越界访问
    if result.empty:
        print("  未扫描出有效的策略组合（数据不足或期间无波动），请拉取更长周期数据后重试。")
        print("=" * 76)
        return result

    # 按组合宽度对齐打印
    header = f"{'排名':<4}{'组合':<14}{'夏普':>7}{'年化%':>9}{'回撤%':>9}{'胜率%':>8}{'盈亏比':>8}{'在市%':>8}"
    print(header)
    print("-" * 76)
    for _, r in result.iterrows():
        print(f"{int(r['排名']):<4}{r['组合']:<14}{r['夏普']:>7.2f}"
              f"{r['年化收益%']:>9.2f}{r['最大回撤%']:>9.2f}"
              f"{r['胜率%']:>8.2f}{r['盈亏比']:>8.2f}{r['在市占比%']:>8.1f}")
    print("=" * 76)

    best = result.iloc[0]
    print(f"  ★ 最优组合：{best['组合']}（夏普 {best['夏普']:.2f}，"
          f"年化 {best['年化收益%']:+.2f}%，最大回撤 {best['最大回撤%']:.2f}%）")
    print("=" * 76)
    return result


def _parse_combo(combo: str):
    """把 'MA5/MA30' 解析成 (5, 30)。"""
    parts = combo.split("/")
    return int(parts[0][2:]), int(parts[1][2:])


def out_of_sample_validation(
    df: pd.DataFrame,
    name: str = "",
    ticker: str = "",
    train_ratio: float = 0.7,
    param_grid: list = None,
) -> pd.DataFrame:
    """样本外验证：前段数据选最优参数，后段（未参与选参）数据检验。

    流程：
    1. 按时间切分：前 train_ratio 为样本内，后 1-train_ratio 为样本外
    2. 样本内跑参数扫描，选夏普最高的组合
    3. 用该组合在样本外回测（信号在完整序列上计算，样本外开头无缝衔接）
    4. 对比样本内/样本外绩效，判断是否存在过拟合
    """
    n = len(df)
    split = int(n * train_ratio)
    train = df.iloc[:split]
    test = df.iloc[split:]

    if len(train) < 60 or len(test) < 30:
        print("\n数据量不足，无法进行样本外验证（样本内需≥60天、样本外需≥30天）。")
        return None

    title = f"{name}（{ticker}）" if name else ticker
    print("\n" + "=" * 76)
    print(f"  样本外验证报告 — {title}")
    print("=" * 76)
    print(f"  样本内(选参) {train.index[0].date()} → {train.index[-1].date()}（{len(train)} 天）")
    print(f"  样本外(检验) {test.index[0].date()} → {test.index[-1].date()}（{len(test)} 天）")
    print("-" * 76)

    # 1) 样本内：参数扫描选最优
    result = parameter_scanner(train, name, ticker, param_grid, verbose=False)
    if result.empty:
        print("样本内扫描无有效结果。")
        return None
    best = result.iloc[0]
    short_win, long_win = _parse_combo(best["组合"])

    # 2) 样本外：用最优参数回测
    bt_full = backtest_ma_strategy(df, short_win, long_win)   # 在完整序列上算信号
    bt_test = bt_full.iloc[split:]                            # 样本外段
    test_metrics = metrics_from_returns(bt_test["strategy_ret"])
    if test_metrics is None:
        print("样本外数据不足，无法计算绩效。")
        return None

    train_metrics = {
        "夏普": best["夏普"],
        "年化收益%": best["年化收益%"],
        "最大回撤%": best["最大回撤%"],
    }

    # 3) 打印对比表
    header = f"{'指标':<12}{'样本内':>12}{'样本外':>12}{'变化':>12}"
    print(header)
    print("-" * 76)
    rows = [
        ("最优组合", best["组合"], best["组合"], "—"),
        ("夏普", f"{train_metrics['夏普']:.2f}",
         f"{test_metrics['sharpe']:.2f}",
         f"{test_metrics['sharpe'] - train_metrics['夏普']:+.2f}"),
        ("年化收益%", f"{train_metrics['年化收益%']:.2f}",
         f"{test_metrics['ann_return'] * 100:.2f}",
         f"{test_metrics['ann_return'] * 100 - train_metrics['年化收益%']:+.2f}"),
        ("最大回撤%", f"{train_metrics['最大回撤%']:.2f}",
         f"{test_metrics['max_drawdown'] * 100:.2f}",
         f"{test_metrics['max_drawdown'] * 100 - train_metrics['最大回撤%']:+.2f}"),
        ("胜率%", f"{best['胜率%']:.2f}", f"{test_metrics['win_rate']:.2f}",
         f"{test_metrics['win_rate'] - best['胜率%']:+.2f}"),
    ]
    for label, a, b, c in rows:
        print(f"{label:<12}{a:>12}{b:>12}{c:>12}")

    # 4) 结论：样本外夏普是否仍为正/是否显著衰减
    print("-" * 76)
    in_sharpe = train_metrics["夏普"]
    out_sharpe = test_metrics["sharpe"]
    if out_sharpe > 0 and out_sharpe >= in_sharpe * 0.5:
        verdict = "✓ 样本外表现稳健，参数较可信（过拟合风险较低）"
    elif out_sharpe > 0:
        verdict = "△ 样本外仍为正收益，但较样本内衰减，存在一定过拟合"
    else:
        verdict = "✗ 样本外表现转差，过拟合风险高，该参数组合需谨慎"
    print(f"  【结论】{verdict}")
    print("=" * 76)

    # 返回样本外绩效摘要
    return pd.DataFrame({
        "最优组合": [best["组合"]],
        "样本内夏普": [in_sharpe],
        "样本外夏普": [out_sharpe],
    })


def plot_best_strategy(
    df: pd.DataFrame,
    short_win: int,
    long_win: int,
    name: str = "",
    ticker: str = "",
) -> None:
    """可视化最优双均线策略：净值曲线对比 + 买卖点标注。

    上图：策略累计净值 vs 买入持有累计净值；
    下图：收盘价与均线，标注金叉(买入▲)/死叉(卖出▼)点位。
    """
    bt = backtest_ma_strategy(df, short_win, long_win)
    close = bt["Close"]

    # 净值曲线（复利累计）
    strat_equity = (1 + bt["strategy_ret"]).cumprod()
    bh_equity = close / close.iloc[0]

    # 找出买卖信号变化点（金叉/死叉发生当日，即 position 从 0→1 或 1→0）
    pos_change = bt["position"].diff().fillna(bt["position"])
    buy_idx = bt.index[pos_change == 1]      # 金叉买入
    sell_idx = bt.index[pos_change == -1]    # 死叉卖出

    title = f"{name}（{ticker}）" if name else ticker
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 10), sharex=True,
        gridspec_kw={"height_ratios": [1, 2]},
    )
    fig.suptitle(f"最优策略可视化（MA{short_win}/MA{long_win}）— {title}",
                 fontsize=15, fontweight="bold")

    # 上图：净值对比
    ax1.plot(strat_equity.index, strat_equity.values, label=f"策略 MA{short_win}/MA{long_win}",
             color="#1f77b4", lw=1.8)
    ax1.plot(bh_equity.index, bh_equity.values, label="买入持有", color="#7f7f7f", lw=1.4, ls="--")
    ax1.axhline(1.0, color="gray", lw=0.8, ls=":")
    ax1.set_title("累计净值对比", fontsize=12)
    ax1.set_ylabel("净值")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    # 下图：价格 + 均线 + 买卖点
    ax2.plot(close.index, close.values, label="收盘价", color="#555555", lw=1.2)
    ax2.plot(bt.index, bt["MA_short"], label=f"MA{short_win}", color="#ff7f0e", lw=1.2)
    ax2.plot(bt.index, bt["MA_long"], label=f"MA{long_win}", color="#d62728", lw=1.2)
    if len(buy_idx):
        ax2.scatter(buy_idx, close.loc[buy_idx], marker="^", s=90,
                    color="#2ca02c", label="金叉买入", zorder=5)
    if len(sell_idx):
        ax2.scatter(sell_idx, close.loc[sell_idx], marker="v", s=90,
                    color="#d62728", label="死叉卖出", zorder=5)
    ax2.set_title(f"价格与买卖信号（MA{short_win} 上穿/下穿 MA{long_win}）", fontsize=12)
    ax2.set_ylabel("价格 (元)")
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    plt.show()


def plot_performance_report(df: pd.DataFrame, name: str = "", ticker: str = "") -> None:
    """绩效可视化：净值曲线、回撤曲线、核心指标条形图与 KPI 速览面板。"""
    metrics = compute_performance_metrics(df)
    if metrics is None:
        print("\n数据不足，无法绘制绩效图。")
        return

    close = df["Close"]
    # 累计收益曲线（%）
    cum_return = (close / close.iloc[0] - 1) * 100
    # 回撤曲线（%）
    drawdown = (close / close.cummax() - 1) * 100

    title = f"{name}（{ticker}）" if name else ticker
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(f"绩效可视化报告 — {title}", fontsize=16, fontweight="bold")

    # 1) 累计收益净值曲线
    ax = axes[0, 0]
    ax.plot(cum_return.index, cum_return.values, color="#1f77b4", lw=1.8)
    ax.axhline(0, color="gray", lw=0.8, ls="--")
    ax.fill_between(cum_return.index, cum_return.values, 0,
                    where=cum_return.values >= 0, color="#1f77b4", alpha=0.15)
    ax.set_title("累计收益率曲线", fontsize=12)
    ax.set_ylabel("累计收益 (%)")
    ax.grid(True, alpha=0.3)

    # 2) 回撤曲线
    ax = axes[0, 1]
    ax.fill_between(drawdown.index, drawdown.values, 0, color="#d62728", alpha=0.45)
    ax.plot(drawdown.index, drawdown.values, color="#d62728", lw=1.2)
    ax.set_title("回撤曲线", fontsize=12)
    ax.set_ylabel("回撤 (%)")
    ax.grid(True, alpha=0.3)

    # 3) 核心指标横向条形图（百分比类指标）
    ax = axes[1, 0]
    labels = ["累计收益", "年化收益", "年化波动", "最大回撤", "胜率"]
    vals = [
        metrics["total_return"] * 100,
        metrics["ann_return"] * 100,
        metrics["ann_vol"] * 100,
        metrics["max_drawdown"] * 100,
        metrics["win_rate"],
    ]
    ypos = list(range(len(labels)))[::-1]
    bar_colors = []
    for label, v in zip(labels, vals):
        if label == "最大回撤":
            bar_colors.append("#d62728")
        elif label == "年化波动":
            bar_colors.append("#ff7f0e")
        else:
            bar_colors.append("#2ca02c" if v >= 0 else "#d62728")
    ax.barh(ypos, vals, color=bar_colors, alpha=0.85, height=0.6)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    ax.axvline(0, color="gray", lw=0.8)
    ax.set_title("核心指标对比（%）", fontsize=12)
    ax.grid(True, axis="x", alpha=0.3)
    scale = max(abs(x) for x in vals) or 1.0
    for y, v in zip(ypos, vals):
        offset = 0.03 * scale
        ax.text(v + (offset if v >= 0 else -offset), y, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right", fontsize=10)

    # 4) KPI 文本速览面板（夏普、盈亏比等）
    ax = axes[1, 1]
    ax.axis("off")
    ax.text(0.05, 0.92, "关键指标速览", fontsize=13, fontweight="bold", transform=ax.transAxes)
    kpi_lines = [
        ("夏普比率", f"{metrics['sharpe']:.2f}", "#1f77b4"),
        ("盈亏比", f"{metrics['profit_loss']:.2f}", "#ff7f0e"),
        ("胜率", f"{metrics['win_rate']:.2f}%", "#2ca02c"),
        ("最大回撤", f"{metrics['max_drawdown'] * 100:.2f}%", "#d62728"),
    ]
    y = 0.72
    for label, value, color in kpi_lines:
        ax.text(0.05, y, label, fontsize=12, transform=ax.transAxes, color="gray")
        ax.text(0.95, y, value, fontsize=12, fontweight="bold",
                transform=ax.transAxes, ha="right", color=color)
        y -= 0.13

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    plt.show()


def plot_combined_analysis(df: pd.DataFrame, name: str, ticker: str) -> None:
    """综合看板：把走势图与绩效图合并为一张 3×2 图（同周期，可对齐）。

    布局：
      左上 价格+均线 | 右上 成交量
      左中 累计收益  | 右中 回撤曲线
      左下 核心指标  | 右下 KPI 速览
    """
    close = df["Close"]
    metrics = compute_performance_metrics(df)
    title = f"{name}（{ticker}）" if name else ticker

    fig, axes = plt.subplots(3, 2, figsize=(16, 13))
    fig.suptitle(f"综合行情分析 — {title}（近 {len(df)} 个交易日）",
                 fontsize=16, fontweight="bold")

    # —— 左上：收盘价 + 均线 ——
    ax = axes[0, 0]
    ax.plot(df.index, df["Close"], label="收盘价", color="#1f77b4", lw=1.6)
    ax.plot(df.index, df["MA5"], label="MA5", color="#ff7f0e", lw=1.2)
    ax.plot(df.index, df["MA20"], label="MA20", color="#d62728", lw=1.2)
    ax.set_title("价格走势（含均线）", fontsize=12)
    ax.set_ylabel("价格 (元)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)

    # —— 右上：成交量（涨红跌绿）——
    ax = axes[0, 1]
    colors = ["#d62728" if c >= o else "#2ca02c"
              for c, o in zip(df["Close"], df["Open"])]
    ax.bar(df.index, df["Volume"], color=colors, alpha=0.6)
    ax.set_title("成交量（手）", fontsize=12)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    # —— 左中：累计收益曲线 ——
    ax = axes[1, 0]
    if metrics:
        cum_return = (close / close.iloc[0] - 1) * 100
        ax.plot(cum_return.index, cum_return.values, color="#1f77b4", lw=1.8)
        ax.axhline(0, color="gray", lw=0.8, ls="--")
        ax.fill_between(cum_return.index, cum_return.values, 0,
                        where=cum_return.values >= 0, color="#1f77b4", alpha=0.15)
    ax.set_title("累计收益率曲线", fontsize=12)
    ax.set_ylabel("累计收益 (%)")
    ax.grid(True, alpha=0.3)

    # —— 右中：回撤曲线 ——
    ax = axes[1, 1]
    if metrics:
        drawdown = (close / close.cummax() - 1) * 100
        ax.fill_between(drawdown.index, drawdown.values, 0, color="#d62728", alpha=0.45)
        ax.plot(drawdown.index, drawdown.values, color="#d62728", lw=1.2)
    ax.set_title("回撤曲线", fontsize=12)
    ax.set_ylabel("回撤 (%)")
    ax.grid(True, alpha=0.3)

    # —— 左下：核心指标条形图 ——
    ax = axes[2, 0]
    if metrics:
        labels = ["累计收益", "年化收益", "年化波动", "最大回撤", "胜率"]
        vals = [
            metrics["total_return"] * 100,
            metrics["ann_return"] * 100,
            metrics["ann_vol"] * 100,
            metrics["max_drawdown"] * 100,
            metrics["win_rate"],
        ]
        ypos = list(range(len(labels)))[::-1]
        bar_colors = []
        for label, v in zip(labels, vals):
            if label == "最大回撤":
                bar_colors.append("#d62728")
            elif label == "年化波动":
                bar_colors.append("#ff7f0e")
            else:
                bar_colors.append("#2ca02c" if v >= 0 else "#d62728")
        ax.barh(ypos, vals, color=bar_colors, alpha=0.85, height=0.6)
        ax.set_yticks(ypos)
        ax.set_yticklabels(labels)
        ax.axvline(0, color="gray", lw=0.8)
        ax.grid(True, axis="x", alpha=0.3)
        scale = max(abs(x) for x in vals) or 1.0
        for y, v in zip(ypos, vals):
            offset = 0.03 * scale
            ax.text(v + (offset if v >= 0 else -offset), y, f"{v:+.1f}%",
                    va="center", ha="left" if v >= 0 else "right", fontsize=9)
    ax.set_title("核心指标对比（%）", fontsize=12)

    # —— 右下：KPI 速览面板 ——
    ax = axes[2, 1]
    ax.axis("off")
    ax.text(0.05, 0.95, "关键指标速览", fontsize=13, fontweight="bold", transform=ax.transAxes)
    if metrics:
        kpi_lines = [
            ("夏普比率", f"{metrics['sharpe']:.2f}", "#1f77b4"),
            ("盈亏比", f"{metrics['profit_loss']:.2f}", "#ff7f0e"),
            ("胜率", f"{metrics['win_rate']:.2f}%", "#2ca02c"),
            ("最大回撤", f"{metrics['max_drawdown'] * 100:.2f}%", "#d62728"),
            ("年化收益", f"{metrics['ann_return'] * 100:+.2f}%", "#1f77b4"),
        ]
        y = 0.76
        for label, value, color in kpi_lines:
            ax.text(0.05, y, label, fontsize=12, transform=ax.transAxes, color="gray")
            ax.text(0.95, y, value, fontsize=12, fontweight="bold",
                    transform=ax.transAxes, ha="right", color=color)
            y -= 0.12

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    plt.show()


def get_ticker_from_user() -> str:
    """交互式让用户输入 A 股代码，并校验格式（6/0/3 开头、6 位数字）。"""
    default = DEFAULT_TICKER
    while True:
        raw = input(f"请输入股票代码（直接回车使用默认 {default}，输入 q 退出）：").strip()
        if raw == "":
            return default
        if raw.lower() in ("q", "quit", "exit"):
            print("已退出。")
            raise SystemExit(0)
        if len(raw) != 6 or not raw.isdigit():
            print("  格式错误：请输入 6 位数字的 A 股代码（例如 600519 或 000001）。\n")
            continue
        if not raw.startswith(("6", "0", "3")):
            print("  暂不支持该代码：仅支持 6/0/3 开头的 A 股（沪市 6、深市 0/3）。\n")
            continue
        return raw


def main() -> None:
    # 无 GUI 环境提示（Agg 后端不弹窗，其余功能不受影响）
    if matplotlib.get_backend().lower() == "agg":
        print("[提示] 当前环境无图形界面，图表将不会弹出（报告与 CSV 仍正常输出）。\n")

    # 优先级：命令行参数 > 交互输入 > 默认值
    if len(sys.argv) > 1:
        ticker = sys.argv[1]
        # 与交互式输入保持一致的校验：6 位数字且 6/0/3 开头
        if (len(ticker) != 6 or not ticker.isdigit()
                or not ticker.startswith(("6", "0", "3"))):
            print("命令行参数格式错误，请用 6 位数字的 A 股代码（6/0/3 开头），例如：python quant1.py 000001")
            ticker = get_ticker_from_user()
    else:
        ticker = get_ticker_from_user()

    try:
        df, name = fetch_kline(ticker)
    except RuntimeError as e:
        print(f"\n[错误] 行情数据下载失败：{e}")
        print("请检查网络连接或稍后重试。")
        return
    df = add_indicators(df)

    # 在终端里也打印出表格，方便不弹图时查看
    print(df.tail(10).to_string())

    latest = df.iloc[-1]
    print(f"\n最新交易日: {df.index[-1].date()}")
    print(f"收盘价: {latest['Close']:.2f} 元")
    print(f"当日涨跌幅: {latest['ChangePct']:+.2f}%")
    print(f"RSI(14): {latest['RSI']:.1f}")
    # 20 日累计涨幅：与 20 个交易日前的收盘价相比（而非昨日）
    if len(df) >= 21:
        close_20d_ago = df.iloc[-21]["Close"]
        print(f"20日累计涨幅: {(latest['Close'] / close_20d_ago - 1) * 100:+.2f}%")
    elif len(df) > 1:
        close_prev = df.iloc[-2]["Close"]
        print(f"累计涨幅(样本不足20日): {(latest['Close'] / close_prev - 1) * 100:+.2f}%")

    # 打印专业绩效报告（量化指标）
    print_performance_report(df, name, ticker)

    # 策略参数扫描（用更长周期的数据，保证长均线有意义）
    print("\n正在拉取更长周期数据用于策略参数扫描 ...")
    try:
        scan_df, _ = fetch_kline(ticker, days=500, max_attempts=2)
        scan_df = add_indicators(scan_df)
        result = parameter_scanner(scan_df, name, ticker)
        # 样本外验证：用前70%选参，后30%检验
        out_of_sample_validation(scan_df, name, ticker)
        # 可视化最优策略（用完整周期扫出的最优组合）
        if not result.empty:
            best_combo = result.iloc[0]["组合"]
            short_win, long_win = _parse_combo(best_combo)
            try:
                plot_best_strategy(scan_df, short_win, long_win, name, ticker)
            except Exception as e:
                print(f"\n最优策略图表打开失败（不影响数据）：{e}")
    except Exception as e:
        print(f"\n参数扫描失败（不影响主流程）：{e}")

    # 保存一份 CSV，方便离线查看
    csv_name = f"{ticker}.csv"
    df.to_csv(csv_name, encoding="utf-8-sig")
    print(f"\n数据已保存到: {csv_name}")

    try:
        plot_combined_analysis(df, name, ticker)
    except Exception as e:
        print(f"\n综合图表打开失败（不影响数据）：{e}")


if __name__ == "__main__":
    main()