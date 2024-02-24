# 中文 era 宇宙

该网站使用 [Hugo](https://gohugo.io/) 框架搭建，相关技术细节请参考官方网站。

## 本地开发

### 环境配置
参见 https://gohugo.io/installation/windows/ ，推荐直接下载预编译二进制程序，或使用 Chocolatey Scoop 或 Winget 直接安装。

### 项目结构

```
root
├─assets (css文件)
├─content (网站主体)
│  └─blog
├─layouts (工具组件等)
│  ├─partials
│  │  ├─custom
│  │  └─third-party
│  ├─shortcodes
│  └─_default
│      └─_markup
├─public (构建结果)
├─resources (构建结果)
├─static (静态资源，图像及CNAME文件等)
│  └─images
└─themes
```