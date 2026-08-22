"""pIES 后端应用包。"""

from importlib.metadata import version

# 产品版本的唯一权威源是安装包元数据(由 backend/pyproject.toml 生成)。
# 缺少元数据时应显式失败，不能用硬编码默认值掩盖打包错误。
__version__ = version("iesplan")
