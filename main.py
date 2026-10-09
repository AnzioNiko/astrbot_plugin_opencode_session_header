"""
astrbot_plugin_opencode_session_header
======================================

为指向 ``https://opencode.ai/zen/go/v1`` 的 LLM 请求自动注入
``x-opencode-session`` 请求头。

请求头的值通常根据当前会话的 Session ID（``unified_msg_origin``）**确定性推导**
为 UUID v4。如果请求上下文没有提供 Session ID，则按插件配置使用进程内随机 UUID，
或使用用户指定的固定 UUID，避免缺少会话上下文时漏注入请求头。

插件只对「当前会话所用模型的 API 端点 == 目标端点」的请求生效，其它端点
（OpenAI / DeepSeek / Ollama 等）完全不受影响，保持原样透传。

本插件：
* 自身不发起任何网络请求；
* 不收集 / 上传 / 落盘任何用户数据；
* 不读取、不修改聊天内容与模型参数。

配置项（由插件目录下的 ``_conf_schema.json`` 提供；若该文件缺失或某项未填写，
一律回退到下面的默认值，插件依然可用）：

* ``enable``                —— 总开关，默认 ``True``；
* ``target_endpoint``       —— 目标端点，默认 ``https://opencode.ai/zen/go/v1``；
* ``header_name``           —— 注入的请求头名，默认 ``x-opencode-session``；
* ``ignore_trailing_slash`` —— 比对端点时忽略末尾 ``/``，默认 ``True``；
* ``log_injection``         —— 注入成功时输出日志，默认 ``True``；
* ``log_skip``              —— 跳过注入时输出日志，默认 ``True``；
* ``missing_session_mode``  —— 缺少会话 ID 时使用随机或固定 UUID，默认 ``random``；
* ``fixed_session_uuid``    —— 固定兜底模式使用的 UUID。

日志级别过滤完全交由 AstrBot 全局 ``log_level`` 处理，本插件不再维护独立的日志
阈值；调整全局 ``log_level`` 即可控制本插件日志的显示。

端点匹配与验证：仅当「当前会话所用模型的 API 端点与 ``target_endpoint`` 完全
一致（可选忽略末尾 ``/``）」时才注入；形如 ``.../v1/chat/completions`` 这类带
路径后缀的 Base URL **不会**匹配。管理员可用 ``/oc-session`` 命令查看当前会话的
Session ID、推导出的 UUID、解析到的端点以及是否匹配。由于 ``ProviderRequest`` 本身
不负责透传 HTTP 请求头，插件会在 OpenAI SDK 的 ``chat.completions.create`` 与
``responses.create`` 实际发包入口追加 ``extra_headers``，并使用协程隔离避免并发会话串值。
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import uuid
from urllib.parse import urlsplit

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 常量 / 默认值（与 _conf_schema.json 保持一致）
# ---------------------------------------------------------------------------
DEFAULT_TARGET_ENDPOINT = "https://opencode.ai/zen/go/v1"
DEFAULT_HEADER_NAME = "x-opencode-session"

# 会话 UUID 缓存上限（Session ID -> UUID；纯函数结果，永不过期）
_UUID_CACHE_MAX = 1024

# 全局上下文与活动实例：低层 OpenAI SDK 拦截器不依赖具体插件是否触发
# on_llm_request，而是直接在真正构造 HTTP 请求时读取当前事件会话。
_CURRENT_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "astrbot_opencode_current_session_id", default=None
)
_ACTIVE_PLUGIN: "OpenCodeSessionHeaderPlugin | None" = None


class OpenCodeSessionHeaderPlugin(Star):
    """OpenCode Session Header 插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # config 由 AstrBot 依据 _conf_schema.json 自动解析传入；
        # 若该文件缺失或某项未配置，下面统一回退到默认值，插件仍可正常使用。
        # 保留传入的 AstrBotConfig 对象（其可能带有 save_config 等实例方法），
        # 仅在完全未传入时才退回普通 dict，避免丢失对象方法。
        self.config: AstrBotConfig = config if config is not None else {}

        # 读取配置（带默认值与类型兜底，避免用户误填导致异常）
        self.enable: bool = self._cfg_bool("enable", True)
        self.target_endpoint: str = self._cfg_str("target_endpoint", DEFAULT_TARGET_ENDPOINT)
        self.header_name: str = self._cfg_str("header_name", DEFAULT_HEADER_NAME)
        self.ignore_trailing_slash: bool = self._cfg_bool("ignore_trailing_slash", True)
        self.log_injection: bool = self._cfg_bool("log_injection", True)
        self.log_skip: bool = self._cfg_bool("log_skip", True)
        self.missing_session_mode: str = self._cfg_str(
            "missing_session_mode", "random"
        ).strip().lower()
        if self.missing_session_mode not in ("random", "fixed"):
            self._emit(
                "WARNING",
                "[opencode_session_header] 缺少会话 ID 的处理模式无效，改用 random",
            )
            self.missing_session_mode = "random"
        self.fixed_session_uuid: str | None = self._canonical_uuid(
            self._cfg_str("fixed_session_uuid", "")
        )
        # 随机模式每次插件加载生成一个 UUID，并在该进程生命周期内复用，
        # 避免无会话上下文的连续请求被拆成多个 OpenCode 会话。
        self._random_fallback_session_uuid: str = str(uuid.uuid4())
        if self.missing_session_mode == "fixed" and not self.fixed_session_uuid:
            self._emit(
                "WARNING",
                "[opencode_session_header] 固定兜底 UUID 为空或格式无效，改用随机模式",
            )
            self.missing_session_mode = "random"

        # 规范化后的目标端点（用于比对）
        self._normalized_target: str = self._normalize_endpoint(self.target_endpoint)

        # 轻量缓存：Session ID -> UUID（纯函数结果，永不过期）。
        self._uuid_cache: dict[str, str] = {}
        # 通过 ContextVar 把当前 on_llm_request 钩子对应的 Session 传递到
        # OpenAI SDK 的实际 create() 调用，避免修改全局 client 默认请求头。
        self._request_header_var: contextvars.ContextVar[tuple[str, str] | None] = (
            contextvars.ContextVar(
                "astrbot_opencode_session_header", default=None
            )
        )
        self._request_sid_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "astrbot_opencode_session_header_sid", default=None
        )
        self._wrapper_install_warned: set[str] = set()
        self._global_hook_records: list[tuple[object, str, object, object]] = []

        # 安装全局低层拦截器。它覆盖 Context.llm_generate()、其它插件直接
        # provider.text_chat() 以及没有触发 on_llm_request 的调用路径。
        global _ACTIVE_PLUGIN
        _ACTIVE_PLUGIN = self
        self._install_global_hooks()

        logger.info(
            f"[opencode_session_header] 插件已加载：enable={self.enable}, "
            f"target_endpoint={self.target_endpoint}, header_name={self.header_name}, "
            f"missing_session_mode={self.missing_session_mode}"
        )


    # ------------------------------------------------------------------ #
    # 全局请求拦截
    # ------------------------------------------------------------------ #
    def _install_global_hooks(self) -> None:
        """在 AstrBot 事件上下文和 OpenAI SDK 的真实发包前安装拦截器。

        仅包装 OpenAI SDK 的 ``_prepare_request``，不修改 AstrBot 核心文件，
        因而所有通过 OpenAI-compatible Provider 发出的同步/异步请求都能覆盖。
        ``ContextVar`` 会随事件任务及其子任务继承，避免并发会话串线。
        """
        self._install_scheduler_hook()
        self._install_openai_prepare_hooks()

    def _install_scheduler_hook(self) -> None:
        try:
            from astrbot.core.pipeline.scheduler import PipelineScheduler
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[opencode_session_header] 无法安装事件会话上下文拦截器：{exc}",
            )
            return

        name = "execute"
        current = getattr(PipelineScheduler, name, None)
        if not callable(current):
            return
        original = getattr(current, "_astrbot_opencode_session_header_original", current)

        async def wrapped(scheduler, event, *args, **kwargs):
            sid = getattr(event, "unified_msg_origin", None)
            token = _CURRENT_SESSION_ID.set(str(sid) if sid else None)
            try:
                return await original(scheduler, event, *args, **kwargs)
            finally:
                _CURRENT_SESSION_ID.reset(token)

        functools.update_wrapper(wrapped, original)
        setattr(wrapped, "_astrbot_opencode_session_header_hook", True)
        setattr(wrapped, "_astrbot_opencode_session_header_original", original)
        setattr(PipelineScheduler, name, wrapped)
        self._global_hook_records.append((PipelineScheduler, name, original, wrapped))

    def _install_openai_prepare_hooks(self) -> None:
        try:
            import openai
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[opencode_session_header] 无法加载 OpenAI SDK 全局拦截器：{exc}",
            )
            return

        # OpenAI SDK 3.x 在具体客户端类（AsyncOpenAI/OpenAI）上覆写了
        # _prepare_request；只包装 APIClient 基类不会命中真实请求。
        client_classes = []
        for class_name in (
            "AsyncOpenAI",
            "OpenAI",
            "AsyncClient",
            "Client",
            "AsyncAzureOpenAI",
            "AzureOpenAI",
            "AsyncBedrockOpenAI",
            "BedrockOpenAI",
        ):
            client_class = getattr(openai, class_name, None)
            if client_class is not None and client_class not in client_classes:
                client_classes.append(client_class)

        for client_class in client_classes:
            original = getattr(client_class, "_prepare_request", None)
            if not callable(original):
                continue
            original = getattr(
                original,
                "_astrbot_opencode_session_header_original",
                original,
            )

            if inspect.iscoroutinefunction(original):
                async def wrapped(client, request, _original=original):
                    plugin = _ACTIVE_PLUGIN
                    if plugin is not None:
                        plugin._inject_httpx_request(request)
                    return await _original(client, request)
            else:
                def wrapped(client, request, _original=original):
                    plugin = _ACTIVE_PLUGIN
                    if plugin is not None:
                        plugin._inject_httpx_request(request)
                    return _original(client, request)

            functools.update_wrapper(wrapped, original)
            setattr(wrapped, "_astrbot_opencode_session_header_hook", True)
            setattr(wrapped, "_astrbot_opencode_session_header_original", original)
            setattr(client_class, "_prepare_request", wrapped)
            self._global_hook_records.append((client_class, "_prepare_request", original, wrapped))

    def _inject_httpx_request(self, request) -> None:
        """在 OpenAI SDK 已构造好的 HTTP 请求上追加请求头。"""
        url = str(getattr(request, "url", "") or "")

        if not self.enable:
            self._log_global_skip("插件已禁用", url)
            return

        if not self._request_url_matches_target(url):
            self._log_global_skip(f"端点不匹配 ({url})", url)
            return

        # 如果更上游已经提供了请求头，保留该值，避免覆盖调用方指定的 Session。
        try:
            existing_header = request.headers.get(self.header_name)
        except Exception:
            existing_header = None
        if existing_header and str(existing_header).strip():
            self._log_global_skip(f"请求已包含 {self.header_name}，保留现有值", url)
            return

        sid = _CURRENT_SESSION_ID.get()
        if not sid:
            # 兼容直接调用 on_llm_request 后立刻发包的旧路径。
            sid = self._request_sid_var.get()

        uuid_str, source = self._resolve_session_uuid(str(sid) if sid else None)
        try:
            request.headers[self.header_name] = uuid_str
        except Exception as exc:
            self._emit(
                "ERROR",
                f"[opencode_session_header] 修改 OpenAI HTTP 请求头失败：{exc} (url={url})",
            )
            return

        if self.log_injection:
            self._emit(
                "INFO",
                "[opencode_session_header] 已修改请求头：将注入 "
                f"{self.header_name}={uuid_str} (来源={source}, sid={sid or '<缺失>'}, url={url})",
            )

    def _log_global_skip(self, reason: str, url: str) -> None:
        if self.log_skip:
            self._emit(
                "INFO",
                f"[opencode_session_header] 未修改请求头：{reason} (url={url})",
            )

    def _request_url_matches_target(self, url: str) -> bool:
        """匹配真实 HTTP URL：允许 OpenCode base URL 后面的 API 路径。"""
        try:
            target = urlsplit(self._normalized_target)
            actual = urlsplit(url)
        except Exception:
            return False
        if not target.scheme or not target.netloc or not actual.scheme or not actual.netloc:
            return False
        if (actual.scheme.lower(), actual.netloc.lower()) != (
            target.scheme.lower(),
            target.netloc.lower(),
        ):
            return False

        target_path = target.path.rstrip("/") if self.ignore_trailing_slash else target.path
        actual_path = actual.path.rstrip("/") if self.ignore_trailing_slash else actual.path
        return actual_path == target_path or actual_path.startswith(target_path + "/")

    def _uninstall_global_hooks(self) -> None:
        for owner, name, original, wrapped in reversed(self._global_hook_records):
            try:
                if getattr(owner, name, None) is wrapped:
                    setattr(owner, name, original)
            except Exception:
                pass
        self._global_hook_records.clear()

    # ------------------------------------------------------------------ #
    # 小工具：安全读取配置
    # ------------------------------------------------------------------ #
    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key, default)
        except (AttributeError, KeyError, TypeError):
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _cfg_str(self, key: str, default: str) -> str:
        try:
            value = self.config.get(key, default)
        except (AttributeError, KeyError, TypeError):
            return default
        if value is None:
            return default
        return str(value)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, message: str) -> None:
        """输出日志；级别过滤统一交由 AstrBot 全局 log_level 处理。"""
        try:
            log_fn = getattr(logger, level.lower(), None) or logger.info
            log_fn(message)
        except Exception:
            # 日志失败绝不能影响请求流程
            pass

    # ------------------------------------------------------------------ #
    # 端点处理
    # ------------------------------------------------------------------ #
    def _normalize_endpoint(self, endpoint: str | None) -> str:
        """规范化端点：去首尾空白；可选地忽略末尾多余的 ``/``。"""
        if not endpoint or not isinstance(endpoint, str):
            return ""
        normalized = endpoint.strip()
        if self.ignore_trailing_slash:
            normalized = normalized.rstrip("/")
        return normalized

    def _endpoint_matches(self, endpoint: str | None) -> bool:
        """判断给定端点是否与目标端点匹配。"""
        normalized = self._normalize_endpoint(endpoint)
        if not normalized:
            return False
        return normalized == self._normalized_target

    # ------------------------------------------------------------------ #
    # 确定性 UUID 推导
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sid_to_uuid(sid: str) -> str:
        """由 Session ID 确定性推导出 UUID v4 格式字符串。

        - 输入相同 → 输出必然相同（不引入任何随机数 / 时间戳）；
        - 输出为 8-4-4-4-12 分组、共 36 字符的标准 UUID v4 书写格式；
        - 版本位固定为 4，变体位符合 RFC 4122。
        """
        digest = hashlib.sha256(sid.encode("utf-8")).digest()
        raw = bytearray(digest[:16])
        # 版本位：设置为 UUID v4
        raw[6] = (raw[6] & 0x0F) | 0x40
        # 变体位：设置为 RFC 4122（10xx）
        raw[8] = (raw[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(raw)))

    def _get_uuid(self, sid: str) -> str:
        """带缓存的 UUID 推导：同一 Session ID 的结果恒定，故缓存不会过期。"""
        cached = self._uuid_cache.get(sid)
        if cached is not None:
            return cached
        derived = self._sid_to_uuid(sid)
        if len(self._uuid_cache) < _UUID_CACHE_MAX:
            self._uuid_cache[sid] = derived
        return derived

    @staticmethod
    def _canonical_uuid(value: str | None) -> str | None:
        """验证并规范化用户配置的 UUID；无效值返回 None。"""
        if not value or not str(value).strip():
            return None
        try:
            return str(uuid.UUID(str(value).strip()))
        except (AttributeError, TypeError, ValueError):
            return None

    def _resolve_session_uuid(self, sid: str | None) -> tuple[str, str]:
        """根据会话 ID 或缺失会话配置，返回可注入的 UUID 与来源说明。"""
        if sid:
            return self._get_uuid(str(sid)), "会话 ID 推导"
        if self.missing_session_mode == "fixed" and self.fixed_session_uuid:
            return self.fixed_session_uuid, "固定兜底"
        return self._random_fallback_session_uuid, "随机兜底"

    # ------------------------------------------------------------------ #
    # 读取当前会话的 API 端点
    # ------------------------------------------------------------------ #
    async def _get_current_provider(self, event: AstrMessageEvent):
        """获取当前会话所使用的模型提供商实例；失败时返回 None。

        优先使用 AstrBot 公开接口 ``Context.get_current_chat_provider_id`` 与
        ``Context.get_provider_by_id``；仅为兼容不同版本，保留
        ``provider_manager.get_provider_by_id`` 作为兜底。
        """
        umo = event.unified_msg_origin
        provider_id = None

        # 1) 获取当前会话使用的 provider id
        # 不假设该方法一定是协程：先调用拿到结果，再按需 await，
        # 避免对同步实现首次 await 抛 TypeError 造成额外开销与日志噪音。
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                result = getter(umo=umo)
            except TypeError:
                # 某些版本可能不支持关键字参数，退回位置参数调用
                try:
                    result = getter(umo)
                except (AttributeError, KeyError, TypeError) as e:
                    self._emit(
                        "DEBUG",
                        "[opencode_session_header] 获取当前会话 Provider ID 失败: "
                        f"{type(e).__name__}: {e} (sid={umo})",
                    )
                    result = None
            except (AttributeError, KeyError) as e:
                self._emit(
                    "DEBUG",
                    "[opencode_session_header] 获取当前会话 Provider ID 失败: "
                    f"{type(e).__name__}: {e} (sid={umo})",
                )
                result = None

            if inspect.isawaitable(result):
                try:
                    result = await result
                except (AttributeError, KeyError, TypeError) as e:
                    self._emit(
                        "DEBUG",
                        "[opencode_session_header] 解析当前会话 Provider ID 失败: "
                        f"{type(e).__name__}: {e} (sid={umo})",
                    )
                    result = None
            provider_id = result

        if not provider_id:
            return None

        # 2) 依据 id 获取 provider 实例
        getters = []
        ctx_get = getattr(self.context, "get_provider_by_id", None)
        if callable(ctx_get):
            getters.append(ctx_get)
        provider_manager = getattr(self.context, "provider_manager", None)
        if provider_manager is not None:
            pm_get = getattr(provider_manager, "get_provider_by_id", None)
            if callable(pm_get):
                getters.append(pm_get)

        for g in getters:
            try:
                provider = g(provider_id)
                if inspect.isawaitable(provider):
                    provider = await provider
            except (AttributeError, KeyError, TypeError) as e:
                self._emit(
                    "DEBUG",
                    "[opencode_session_header] 获取 Provider 实例失败: "
                    f"{type(e).__name__}: {e} (sid={umo})",
                )
                continue
            if provider is not None:
                return provider
        return None

    @staticmethod
    def _extract_endpoint(provider) -> str | None:
        """从 provider 实例中提取 API 端点 URL。"""
        if provider is None:
            return None

        candidates: list[str] = []

        # provider.provider_config / provider.config 一般为 dict
        for cfg_attr in ("provider_config", "config"):
            config = getattr(provider, cfg_attr, None)
            if isinstance(config, dict):
                for key in ("api_base", "apiBase", "base_url", "baseUrl", "endpoint", "url"):
                    value = config.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value)

        # 直接挂在 provider 上的属性
        for attr in ("api_base", "base_url", "endpoint"):
            value = getattr(provider, attr, None)
            if isinstance(value, str) and value.strip():
                candidates.append(value)

        return candidates[0] if candidates else None

    async def _get_session_endpoint(self, event: AstrMessageEvent) -> str | None:
        """获取当前会话所用的 API 端点；失败时返回 None。

        每次请求都重新读取当前 Provider，避免同一会话切换模型后仍使用旧端点。
        """
        umo = event.unified_msg_origin
        provider = await self._get_current_provider(event)
        if provider is None:
            self._emit(
                "DEBUG",
                f"[opencode_session_header] 无法获取当前会话的 Provider 实例 (sid={umo})",
            )
            endpoint = None
        else:
            endpoint = self._extract_endpoint(provider)
            if not endpoint:
                # 常见情形（例如非目标端点 / 版本差异），降为 DEBUG 避免日志噪音
                self._emit(
                    "DEBUG",
                    "[opencode_session_header] 已获取 Provider 实例，但未能从公开属性中"
                    f"解析出 API 端点，本插件对该会话不生效 (sid={umo})",
                )

        return endpoint

    # ------------------------------------------------------------------ #
    # 请求头注入
    # ------------------------------------------------------------------ #
    def _make_create_wrapper(self, original, path: str):
        """给 OpenAI SDK 的实际请求方法增加按协程隔离的请求头。

        ProviderRequest 本身只是 AstrBot 的业务数据类，当前版本不会把动态
        添加的 ``extra_headers`` 字段继续传给 OpenAI SDK。因此不能只修改 req；
        必须在 ``client.chat.completions.create`` / ``client.responses.create``
        这两个真实发包入口处追加 OpenAI SDK 支持的 ``extra_headers`` 参数。
        ContextVar 让并发会话各自携带自己的 UUID，不会互相覆盖 client 的全局状态。
        """
        marker = "_astrbot_opencode_session_header_wrapper"
        if getattr(original, marker, False):
            return original

        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            header = self._request_header_var.get()
            if header is not None:
                name, value = header
                extra_headers = kwargs.get("extra_headers")
                if extra_headers is None:
                    extra_headers = {}
                elif not isinstance(extra_headers, dict):
                    try:
                        extra_headers = dict(extra_headers)
                    except (TypeError, ValueError):
                        extra_headers = {}
                else:
                    extra_headers = dict(extra_headers)
                # 请求调用方已显式提供有效值时优先保留，不覆盖已有 Session。
                has_existing = any(
                    str(key).lower() == name.lower() and str(item).strip()
                    for key, item in extra_headers.items()
                )
                if not has_existing:
                    extra_headers[name] = value
                kwargs["extra_headers"] = extra_headers
            result = original(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        setattr(wrapped, marker, True)
        setattr(wrapped, "_astrbot_opencode_session_header_path", path)
        return wrapped

    def _install_request_wrappers(self, provider) -> bool:
        """在当前 provider 的 OpenAI SDK client 上安装实际发包拦截器。"""
        client = getattr(provider, "client", None)
        if client is None:
            return False

        installed = False
        targets = (
            ("chat", "completions", "chat.completions.create"),
            ("responses", None, "responses.create"),
        )
        for first, second, path in targets:
            try:
                resource = getattr(client, first)
                if second is not None:
                    resource = getattr(resource, second)
                original = getattr(resource, "create", None)
                if not callable(original):
                    continue
                if getattr(original, "_astrbot_opencode_session_header_wrapper", False):
                    installed = True
                    continue
                wrapped = self._make_create_wrapper(original, path)
                setattr(resource, "create", wrapped)
                installed = True
            except (AttributeError, TypeError, ValueError) as exc:
                if path not in self._wrapper_install_warned:
                    self._wrapper_install_warned.add(path)
                    self._emit(
                        "WARNING",
                        f"[opencode_session_header] 无法挂接 {path}: {exc}",
                    )
        return installed

    # ------------------------------------------------------------------ #
    # 钩子：LLM 请求前拦截
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在真实 OpenAI SDK 发包入口追加 x-opencode-session。"""
        sid = getattr(event, "unified_msg_origin", None) or getattr(req, "session_id", "") or ""
        sid = str(sid)
        _CURRENT_SESSION_ID.set(sid or None)
        self._request_sid_var.set(sid or None)

        # 每次钩子调用先清空，确保非目标端点不会继承上一次请求的 Session。
        self._request_header_var.set(None)

        if not self.enable:
            self._emit(
                "INFO",
                f"[opencode_session_header] 未修改请求头：插件已禁用 (sid={sid})",
            )
            return

        endpoint = None
        try:
            endpoint = await self._get_session_endpoint(event)
        except Exception as exc:
            self._emit(
                "WARNING",
                f"[opencode_session_header] 获取当前会话端点失败：{exc} (sid={sid})",
            )

        if not endpoint:
            self._emit(
                "INFO",
                f"[opencode_session_header] 未修改请求头：无法解析 API 端点 (sid={sid})",
            )
            return

        if not self._endpoint_matches(endpoint):
            self._emit(
                "INFO",
                "[opencode_session_header] 未修改请求头：当前端点 "
                f"{endpoint} 不匹配 {self.target_endpoint} (sid={sid})",
            )
            return

        try:
            provider = await self._get_current_provider(event)
            if provider is None or not self._install_request_wrappers(provider):
                self._emit(
                    "ERROR",
                    "[opencode_session_header] 未修改请求头：无法挂接 OpenAI SDK 实际发包入口 "
                    f"(sid={sid}, endpoint={endpoint})",
                )
                return
        except Exception as exc:
            self._emit(
                "ERROR",
                f"[opencode_session_header] 安装请求头注入器失败：{exc} (sid={sid})",
            )
            return

        uuid_str, source = self._resolve_session_uuid(sid or None)
        self._request_header_var.set((self.header_name, uuid_str))
        self._emit(
            "INFO",
            "[opencode_session_header] 已修改请求头：将注入 "
            f"{self.header_name}={uuid_str} (来源={source}, sid={sid or '<缺失>'}, endpoint={endpoint})",
        )

    # ------------------------------------------------------------------ #
    # 命令：/oc-session
    # ------------------------------------------------------------------ #
    @filter.command("oc-session")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def oc_session(self, event: AstrMessageEvent):
        """查看当前会话对应的 x-opencode-session UUID 以及端点匹配状态。

        仅管理员可用：输出中包含 Session ID（unified_msg_origin）与内部端点，
        避免在群聊中向普通成员暴露会话标识。
        """
        sid = event.unified_msg_origin
        uuid_str = self._get_uuid(sid)

        endpoint: str | None = None
        try:
            endpoint = await self._get_session_endpoint(event)
        except Exception as e:
            logger.debug(
                f"[opencode_session_header] /oc-session 获取端点失败: {e} (sid={sid})"
            )

        matched = self._endpoint_matches(endpoint)

        lines = [
            f"Session ID: {sid}",
            f"{self.header_name}: {uuid_str}",
        ]
        if endpoint:
            lines.append(f"当前端点: {endpoint}")
        else:
            lines.append("当前端点: <无法解析>")

        if self.enable:
            if matched:
                lines.append(
                    "端点匹配: 是（该会话的请求将被注入 "
                    f"{self.header_name}）"
                )
            else:
                lines.append("端点匹配: 否（本插件不会修改该会话的请求头）")
        else:
            lines.append("插件状态: 已禁用（不会注入请求头）")

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 卸载 / 禁用时调用
    # ------------------------------------------------------------------ #
    async def terminate(self):
        """插件被禁用或卸载时调用。本插件不写入任何持久化数据，无需清理。"""
        global _ACTIVE_PLUGIN
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
        self._uninstall_global_hooks()
        self._emit("INFO", "[opencode_session_header] 插件已卸载/禁用。")