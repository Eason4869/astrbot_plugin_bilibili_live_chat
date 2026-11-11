# 第三方说明

本插件主体采用仓库中的 AGPL-3.0 许可。

## blivedm

Web 弹幕的认证字段、包头/操作码、WBI 顺序常量及业务字段参考 blivedm 的实现，连接监督、限额解码及插件集成由本项目实现。未安装或打包同名 PyPI 包。

- 项目：https://github.com/xfgryujk/blivedm
- 参考文件：`blivedm/clients/web.py`、`blivedm/clients/ws_base.py`、`blivedm/models/web.py`
- 许可：https://github.com/xfgryujk/blivedm/blob/dev/LICENSE

MIT License

Copyright (c) 2018 xfgryujk

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Brotli

Brotli 通过依赖安装，未将其程序文件复制进插件 ZIP。采用 1.2 起公开的有界流式解压接口，保留该依赖自带的许可信息。

- 项目：https://github.com/google/brotli
- 接口参考：https://github.com/google/brotli/blob/v1.2.0/python/_brotli.c
