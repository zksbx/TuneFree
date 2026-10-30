# -*- coding: utf-8 -*-
"""版本号与仓库信息的唯一来源。

发新版只需要改这里的 VERSION（或者让 CI 用 tag 覆写它），
TuneFree.py（界面）和 main.py（后端）都从这里读，不会出现两处版本号不一致。
"""

VERSION = "2.1.2"

GITHUB_REPO = "zksbx/TuneFree"
GITHUB_URL = "https://github.com/" + GITHUB_REPO
GITHUB_RELEASES_URL = GITHUB_URL + "/releases"
GITHUB_LATEST_API = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest"
GITHUB_LATEST_PAGE = GITHUB_RELEASES_URL + "/latest"

# 备用更新源：GitHub API 在国内经常连不上，这里读仓库里的 version.json
VERSION_JSON_URLS = [
    "https://cdn.jsdelivr.net/gh/" + GITHUB_REPO + "@main/version.json",
    "https://cdn.jsdelivr.net/gh/" + GITHUB_REPO + "@master/version.json",
    "https://raw.githubusercontent.com/" + GITHUB_REPO + "/main/version.json",
]

QQ_GROUP = "1067975918"

# 官网。源码在仓库 docs/ 目录，页面文件名是 tunefree.html
SITE_URL = "https://zksbx.github.io/TuneFree/"

# 国内镜像（EdgeOne Makers / Cloudflare Pages 等）。
# 填了之后程序内的「帮助」和「查看更新」优先走它，留空就用 SITE_URL。
SITE_URL_CN = ""

# 程序里统一用的官网地址
SITE_PRIMARY = SITE_URL_CN or SITE_URL

# 站内页面本身（根目录的 index.html 只是跳转壳，用 URL 带 #锚点 时必须写全文件名）
SITE_PAGE = SITE_PRIMARY.rstrip("/") + "/tunefree.html"

# 「查看更新」按钮跳转的地址，想改蓝奏云就填蓝奏云链接
DOWNLOAD_URL = SITE_PAGE + "#download"

