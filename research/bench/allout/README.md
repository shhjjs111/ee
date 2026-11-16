# 全力以赴基准（第三批）

**默认不花钱**：`python scripts/allout_bench.py` 只做 dry-run（校验夹具、打印将执行的命令）。

要真的跑（会调用真实模型、产生费用）：

```bash
python scripts/allout_bench.py --run --yes \
  --model <模型别名> --budget 0.5 --timeout 600 --out bench.json
```

## 夹具格式

```
<slug>/
  task.md    给两种模式的任务说明（普通模式只收到它）
  seed/      起始代码（复制到临时项目并 git init/commit）
  public/    公开验收（模型可见；只有火力全开把它作为判据）
  hidden/    隐藏验收（跑完后在独立目录执行，用 PYTHONPATH 指回项目，模型碰不到）
```

`99_fanout_smoke`（判据恒失败）与 `90_two_winners`（判据前 3 次人为失败）都是
**机器路径冒烟**，用来稳定触发多路分叉，不属于质量样本，统计时排除；其余四个覆盖设计评审要的四类任务：
`01_first_pass`（首轮通过）、`02_small_repair`（小修）、
`03_wrong_approach`（初始实现走错、需要换思路）、
`04_green_but_missing`（公开测试全绿但需求遗漏）。

## 口径

- 独立通过率 = 隐藏用例通过的样本 / 样本数；
- 复验一致性 = 声明的 `met` 与隐藏通过是否一致；
- 每解决任务成本 = 全部样本花费 / 隐藏通过数（无成功样本时只报总支出）；
- 超时与金额缺失单独标记，不混进成功率。

**证据缺口**：本仓只交付脚本与夹具；真实模型对照需要显式 `--run --yes` 才会执行，
结果（含超时与金额缺失）应写进 `research/plans/2026-11-15-allout-upgrade.md` 的对应章节。
