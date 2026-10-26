读文件或列目录；PDF、Word、Excel、PPT 自动转成文字。每行前有「行号+制表符」，默认读 2000 行，用 offset/limit 续读。

## 规则
- 读文件用 read，不用 cat/type/Get-Content，Office/PDF 也不要写脚本去读；返回「与上次读取相同」时文件没变，要全文再读一次。
