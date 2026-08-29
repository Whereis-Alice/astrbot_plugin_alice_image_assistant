"""图片搜索插件常量定义."""

# ==============================================================================
# 网络请求相关
# ==============================================================================

# HTTP 请求超时时间 (秒)
HTTP_TIMEOUT_SECONDS = 30

# 下载图片超时时间 (秒)
IMAGE_DOWNLOAD_TIMEOUT = 20

# 单个策略的总超时时间 (秒)
# ascii2d 内部有 3 次串行请求、各自 30s，不加总超时时单次搜图可能挂 90s 以上
DEFAULT_TOTAL_TIMEOUT_SECONDS = 45
MIN_TOTAL_TIMEOUT_SECONDS = 10
MAX_TOTAL_TIMEOUT_SECONDS = 300

# 默认 User-Agent (与 curl_cffi impersonate chrome120 保持一致)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 模拟浏览器的完整请求头
BROWSER_HEADERS = {"User-Agent": DEFAULT_USER_AGENT}

# ==============================================================================
# 展示相关
# ==============================================================================

# 最终展示结果上限的默认值
DEFAULT_MAX_RESULTS = 5

# 每个引擎的最小抓取条数
# 抓取上限必须 >= 展示上限，否则跨引擎去重后可能凑不满展示条数
MIN_PER_ENGINE_FETCH = 5

# ==============================================================================
# 搜图命令名
# ==============================================================================

# 搜图命令主名称 (与 main.py 中 @filter.command 注册的名称保持一致)
REVERSE_SEARCH_COMMAND = "aa溯"

# 搜图命令别名 (当前未注册别名；新增别名时同时补到这里)
REVERSE_SEARCH_COMMAND_ALIASES: tuple[str, ...] = ()

# 命令主名称 + 全部别名，供“事件是否为搜图命令”判断使用
REVERSE_SEARCH_COMMAND_NAMES: tuple[str, ...] = (
    REVERSE_SEARCH_COMMAND,
    *REVERSE_SEARCH_COMMAND_ALIASES,
)

# ==============================================================================
# SauceNAO 策略
# ==============================================================================

# SauceNAO API 地址
SAUCENAO_BASE_URL = "https://saucenao.com/search.php"

# SauceNAO 单次请求返回条数默认值
DEFAULT_SAUCENAO_NUMRES = 5

# SauceNAO 相似度阈值默认值
# 40 偏低容易误命中 (低相似度命中会污染跨引擎排序)，默认提到 60
DEFAULT_SAUCENAO_SIMILARITY_THRESHOLD = 60

# ==============================================================================
# Google Lens 策略
# ==============================================================================

# SerpAPI 基础 URL
SERPAPI_BASE_URL = "https://serpapi.com"

# Google Lens 单次取用的 visual_matches 条数默认值
DEFAULT_GOOGLE_LENS_MAX_RESULTS = 8

# 单个 SerpAPI Key 请求失败后的冷却时间 (秒)
SERPAPI_KEY_PENALTY_SECONDS = 60

# ==============================================================================
# Ascii2d 策略
# ==============================================================================

# Ascii2d 基础 URL
ASCII2D_BASE_URL = "https://ascii2d.net"

# Ascii2d 搜索 URL
ASCII2D_SEARCH_URI_URL = f"{ASCII2D_BASE_URL}/search/uri"

# Ascii2d authenticity_token 缓存有效期 (秒)
ASCII2D_TOKEN_TTL_SECONDS = 600

# Ascii2d 单次搜索返回条数默认值 (bovw + color 去重后)
DEFAULT_ASCII2D_MAX_RESULTS = 5

# ==============================================================================
# 结果来源标识
# ==============================================================================

# 归一化的来源键 (写入 SearchResultItem.source_key，用于统一排序与融合)
SOURCE_KEY_SAUCENAO = "saucenao"
SOURCE_KEY_GOOGLE_LENS = "google_lens"
SOURCE_KEY_ASCII2D = "ascii2d"
SOURCE_KEY_ASCII2D_BOVW = "ascii2d/bovw"
SOURCE_KEY_ASCII2D_COLOR = "ascii2d/color"

# 来源可信度系数：把“位次分数”折算成跨引擎可比的置信度
# SauceNAO 直接给相似度，可信度最高；ascii2d bovw (特征) 强于 color (配色)；
# Google Lens 只是视觉相似列表，最弱。
SOURCE_CONFIDENCE: dict[str, float] = {
    SOURCE_KEY_SAUCENAO: 1.0,
    SOURCE_KEY_ASCII2D_BOVW: 0.85,
    SOURCE_KEY_ASCII2D: 0.8,
    SOURCE_KEY_ASCII2D_COLOR: 0.7,
    SOURCE_KEY_GOOGLE_LENS: 0.6,
}

# 未知来源的可信度系数
DEFAULT_SOURCE_CONFIDENCE = 0.5

# 来源优先级 (数值越小越优先)，用于分数相同时的稳定排序
SOURCE_PRIORITY: dict[str, int] = {
    SOURCE_KEY_SAUCENAO: 0,
    SOURCE_KEY_ASCII2D_BOVW: 1,
    SOURCE_KEY_ASCII2D: 2,
    SOURCE_KEY_ASCII2D_COLOR: 3,
    SOURCE_KEY_GOOGLE_LENS: 4,
}

# 未知来源的优先级
DEFAULT_SOURCE_PRIORITY = 9

# 展示名称 -> 归一化来源键 (兼容只有 source 字段的旧结果)
SOURCE_NAME_TO_KEY: dict[str, str] = {
    "saucenao": SOURCE_KEY_SAUCENAO,
    "google lens": SOURCE_KEY_GOOGLE_LENS,
    "googlelens": SOURCE_KEY_GOOGLE_LENS,
    "google": SOURCE_KEY_GOOGLE_LENS,
    "ascii2d": SOURCE_KEY_ASCII2D,
}

# ==============================================================================
# 策略名称映射
# ==============================================================================

# 策略名称映射 (小写 -> 策略类名关键字)
STRATEGY_ALIAS_MAP = {
    "saucenao": "SauceNAO",
    "sauce": "SauceNAO",
    "google": "Google Lens",
    "googlelens": "Google Lens",
    "ascii2d": "Ascii2d",
    "ascii": "Ascii2d",
    "2d": "Ascii2d",
}
