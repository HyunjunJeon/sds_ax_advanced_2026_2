"""Pure ranked resolution and deep-merge logic for layered configuration.

The ranked engine is intentionally unaware of the manifest, UI, model, theme,
environment, or filesystem. Providers coerce their own domains before handing
`Found`, `Unset`, or `Invalid` results to this module. Human-readable source
labels likewise remain in `ProviderStatus`; provenance and health here use only
numeric ranks.
"""

# [해설] 이 모듈의 역할: 계층형 설정의 "순수 엔진". 여러 provider(managed/CLI/reload/env/user/default)가 이미 타입 변환한
# [해설] 결과(Found/Unset/Invalid)를 숫자 rank로 정렬해 하나의 값으로 해석하고, 출처(provenance)·계층 건강 상태를 함께 돌려준다.
# [해설] 매니페스트·UI·파일시스템을 모르고 rank 숫자만 안다(docstring 참고). 숫자가 작을수록 우선순위가 강하다.
# [해설] 실행 프로세스: 둘 다(클라이언트·서버 각각 자기 프로세스 캐시 resolver를 가진다).
# [해설] 주요 진입점 심볼: get_config_resolver(프로세스 공유 resolver), ConfigResolver.get/resolve_options/reload_with_replacements,
# [해설] resolver_from_snapshots(표준 체인 생성), install_cli_provider(argparse 후 CLI 계층 삽입), resolve_ranked, merge_toml_tables.
# [해설] 호출자: config.py, config_manifest.py(get_option과 함께), app.py, agent.py, server_graph.py, _server_config.py,
# [해설] configuration/service.py(managed 병합), mcp_disabled.py·model_config.py(resolve_ranked 직접 사용) 등.
# [해설] provider 구현은 configuration/providers.py(TomlFileProvider, EnvProvider, DefaultProvider).
# [해설] 관련 분석 문서: analysis/03-config-models-credentials.md
# [해설] 관련 공식 문서: docs_official/code/configuration.md, docs_official/code/config-file.md
# [해설][주의] 체인에 "프로젝트 config.toml" 계층이 없다(resolver_from_snapshots 참고). 프로젝트 범위는 .env/.deepagents/로만 들어온다(analysis/03 참고).
from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from deepagents_code.config_manifest import ConfigOption
    from deepagents_code.configuration.provider import ConfigProvider
    from deepagents_code.configuration.types import TomlSnapshot

from deepagents_code.configuration.types import (
    Found,
    ProviderResult,
    ProviderStatus,
)

# [해설] rank 상수표(작을수록 강함). 표준 체인 순서: MANAGED 200 → CLI 300 → RELOAD 350 → ENV 400 → USER 500 → DEFAULT 1000.
# [해설] MANAGED_RANK: 관리자(조직) 정책 config. configuration/service.get_managed_snapshot이 제공.
MANAGED_RANK = 200
"""Managed policy rank; lower numeric ranks have stronger precedence."""

# [해설] CLI_RANK: argparse 결과 provider. 프로세스 시작 후 install_cli_provider/ConfigResolver.install_provider로 늦게 삽입.
CLI_RANK = 300
"""Parsed command-line argument rank."""

# [해설] RELOAD_RANK: config.py의 _ReloadOverrideProvider. `/reload` 후 새 generation이 재현하지 못하는 런타임 값
# [해설] (shell.allow_list, skills.extra_allowed_dirs)을 보존하는 계층. config._resolver_with_reload_overrides가 설치.
RELOAD_RANK = 350
"""Retained runtime-reload value rank."""

# [해설] ENVIRONMENT_RANK: EnvProvider(프로세스 env, DEEPAGENTS_CODE_* 접두 우선). durable=False(프로세스와 함께 사라짐).
ENVIRONMENT_RANK = 400
"""Process-environment rank."""

# [해설] USER_RANK: `~/.deepagents/config.toml`(TomlFileProvider, durable=True).
USER_RANK = 500
"""User `config.toml` rank."""

# [해설] DEFAULT_RANK: 매니페스트에 선언된 타입 있는 기본값(DefaultProvider).
DEFAULT_RANK = 1000
"""Typed manifest-default rank."""


# [해설] provider 하나가 옵션 하나에 대해 낸 결과. durable은 "영속 계층인가"(파일=True, env=False)로 replace 전략의 masked 계산에 쓰인다.
# [해설] diagnostics는 같은 계층 안에서 별칭(alias) 키를 시도하며 생긴 경고.
@dataclass(frozen=True, slots=True)
class RankedProviderValue[T]:
    """One provider's already-coerced result for an option."""

    rank: int
    durable: bool
    status: ProviderStatus
    result: ProviderResult[T]
    diagnostics: tuple[str, ...] = ()
    """Ordered warnings encountered while trying aliases inside this tier."""


# [해설] 해석 결과. value와 함께 rank별 provenance(어느 계층이 어떤 leaf 경로를 기여했나), tier_health, provider_status,
# [해설] masked_ranks, selected_ranks를 담는다. `dcode config` 출처 표시(config_manifest._ranked_source)가 이 값을 인덱싱한다.
@dataclass(frozen=True, slots=True)
class ResolvedValue[T]:
    """Resolved value with rank-keyed provenance and provider health.

    Six of the seven fields are parallel rank-keyed collections whose mutual
    consistency is the entire meaning of the type, and consumers index straight
    into them: `config_manifest._ranked_source` reads
    `provider_status[rank] for rank in ranks` to render the source column, so
    an inconsistent instance is a `KeyError` in user-facing output. The
    invariants are checked at construction rather than documented, following
    `TomlSnapshot` in the same package.
    """

    value: T
    provenance: Mapping[int, frozenset[tuple[str, ...]]]
    tier_health: Mapping[int, ProviderResult[T]]
    provider_status: Mapping[int, ProviderStatus]
    masked_ranks: frozenset[int] = frozenset()
    selected_ranks: tuple[int, ...] = ()
    tier_diagnostics: Mapping[int, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    # [해설] 생성 시 불변식 검증: 기여 rank는 모두 provider_status에 있어야 하고, selected와 masked는 겹치면 안 된다.
    def __post_init__(self) -> None:
        """Reject a value whose rank-keyed halves disagree.

        Also copies each mapping behind a `MappingProxyType`. `frozen=True`
        protects the field bindings, not the contents: without the copy a
        caller keeps a live reference to a dict this type presents as a
        read-only snapshot.

        Raises:
            ValueError: If a contributing rank is missing provider status, or
                is also reported as masked.
        """
        # [해설][흐름] 1) 모든 매핑을 dict 복사 후 MappingProxyType으로 감싸 진짜 읽기 전용 스냅샷으로 만든다.
        for name in (
            "provenance",
            "tier_health",
            "provider_status",
            "tier_diagnostics",
        ):
            frozen = MappingProxyType(dict(getattr(self, name)))
            object.__setattr__(self, name, frozen)
        # Both halves of `ranks`, not just `selected_ranks`: it falls back to
        # `provenance` when no rank was selected, and `_ranked_source` indexes
        # `provider_status` by whichever half it gets. Validating one branch
        # left the documented failure mode reachable through the other.
        # [해설][흐름] 2) selected ∪ provenance의 rank가 provider_status에 모두 있는지 → 없으면 렌더링 시 KeyError이므로 미리 거부.
        contributing = set(self.selected_ranks) | set(self.provenance)
        missing = contributing - self.provider_status.keys()
        if missing:
            msg = (
                f"contributing ranks {sorted(missing)} have no provider "
                "status; rendering provenance would raise KeyError"
            )
            raise ValueError(msg)
        both = self.masked_ranks & set(self.selected_ranks)
        if both:
            msg = f"ranks {sorted(both)} cannot be both selected and masked"
            raise ValueError(msg)

    # [해설] 기여 rank 목록. selected_ranks가 있으면 그것, 없으면 provenance의 rank를 정렬.
    @property
    def ranks(self) -> tuple[int, ...]:
        """Contributing ranks in precedence order."""
        return self.selected_ranks or tuple(sorted(self.provenance))


# [해설] 옵션 식별자 타입 별칭(예: "skills.extra_allowed_dirs").
type ConfigKey = str
"""Canonical dotted manifest key."""


# [해설] rank 순으로 정렬된 provider 체인 + 재진입 가능 lock. 프로세스당 하나가 get_config_resolver로 공유된다.
# [해설][설계] 모든 읽기/교체를 같은 lock으로 묶어 "한 번의 읽기는 한 generation"을 보장한다.
class ConfigResolver:
    """Resolve manifest options through an ordered provider chain."""

    # [해설] provider를 rank로 정렬하고 중복 rank는 거부(같은 rank면 우선순위가 모호해짐).
    def __init__(self, providers: Sequence[ConfigProvider]) -> None:
        """Build a resolver with providers sorted by precedence.

        Args:
            providers: Configuration providers with unique numeric ranks.

        Raises:
            ValueError: If two providers declare the same rank.
        """
        ordered = tuple(sorted(providers, key=lambda provider: provider.rank))
        ranks = tuple(provider.rank for provider in ordered)
        if len(set(ranks)) != len(ranks):
            msg = "config providers must have unique ranks"
            raise ValueError(msg)
        self._providers = ordered
        self._lock = threading.RLock()

    # [해설] 옵션 하나를 전체 체인으로 해석. 가장 흔한 호출 경로(get_config_resolver().get(option)).
    def get[T](self, option: ConfigOption[T]) -> ResolvedValue[T]:
        """Resolve one option through every provider.

        Args:
            option: Manifest option to resolve.

        Returns:
            Resolved value with rank-keyed provenance and health.
        """
        with self._lock:
            return self._resolve(option, self._providers)

    # [해설] 특정 rank를 뺀 체인으로 해석. 예: config._sync_reload_overrides가 RELOAD_RANK 없이 값을 다시 계산해
    # [해설] 새 generation이 같은 값을 재현하는지 비교한다.
    def get_without_ranks[T](
        self, option: ConfigOption[T], ranks: Collection[int]
    ) -> ResolvedValue[T]:
        """Resolve one option after excluding selected provider ranks.

        Args:
            option: Manifest option to resolve.
            ranks: Provider ranks to omit from this read.

        Returns:
            Resolved value from the remaining providers.
        """
        with self._lock:
            providers = tuple(
                provider for provider in self._providers if provider.rank not in ranks
            )
            return self._resolve(option, providers)

    # [해설] 실제 해석 로직(lock은 호출자가 잡는다). 병합 전략(replace/union/deep_merge)은 옵션 매니페스트에 선언돼 있다.
    @staticmethod
    def _resolve[T](
        option: ConfigOption[T],
        providers: Sequence[ConfigProvider],
    ) -> ResolvedValue[T]:
        """Resolve one option against a lock-held provider generation.

        Args:
            option: Manifest option to resolve.
            providers: Providers frozen to one generation.

        Returns:
            Resolved value with rank-keyed provenance and health.

        Raises:
            RuntimeError: If no provider returns a value.
        """
        # [해설][흐름] 1) 각 provider가 옵션을 자기 도메인에서 읽고 강제 변환한 RankedProviderValue를 만든다.
        values = tuple(provider.get(option) for provider in providers)
        # [해설][흐름] 2) 누적 전략(union/deep_merge)에서는 DEFAULT 계층을 뺀다 → 기본 목록/테이블이 명시값에 섞여 들어가지 않게.
        strategy = option.merge_strategy.value
        effective_values = (
            tuple(value for value in values if value.rank != DEFAULT_RANK)
            if strategy in {"union", "deep_merge"}
            else values
        )
        # [해설][흐름] 3) rank 기반 해석.
        resolved = resolve_ranked(effective_values, strategy=strategy)
        # [해설][흐름] 4) Found가 하나도 없으면(누적 전략에서 명시값이 전혀 없을 때 등) 새 DefaultProvider 값으로 한 번 더 해석.
        if resolved is None:
            from deepagents_code.configuration.providers import DefaultProvider

            fallback = DefaultProvider().get(option)
            without_default = tuple(
                value for value in values if value.rank != DEFAULT_RANK
            )
            resolved = resolve_ranked(
                (*without_default, fallback),
                strategy=strategy,
            )
        # [해설] 기본값조차 없으면 매니페스트 결함이므로 RuntimeError.
        if resolved is None:
            msg = f"fallback provider was unset for {option.key}"
            raise RuntimeError(msg)
        return resolved

    # [해설] 여러 옵션을 lock 한 번 안에서 해석 → 한 화면/한 기능이 서로 다른 generation 값을 섞어 보지 않게 한다.
    def resolve_options(
        self,
        options: Sequence[ConfigOption[object]],
    ) -> Mapping[ConfigKey, ResolvedValue[object]]:
        """Resolve selected options against one provider generation.

        Resolving an option is not uniformly cheap: `THEME_DELEGATE` reaches
        the theme registry, which imports Textual (~470ms). Callers on the
        startup hot path ask for the options they need rather than the whole
        manifest, and keep the single-generation guarantee either way.

        Args:
            options: Manifest options to resolve together.

        Returns:
            Immutable mapping from canonical option key to resolved value.
        """
        with self._lock:
            resolved = {
                option.key: self._resolve(option, self._providers) for option in options
            }
        return MappingProxyType(resolved)

    # [해설] 매니페스트 전체 해석(`dcode config` 전체 출력 등). Textual import 등 비용이 커서 시작 경로에서는 피한다.
    def resolve_all(self) -> Mapping[ConfigKey, ResolvedValue[object]]:
        """Resolve the full manifest against one provider generation.

        Returns:
            Immutable mapping from canonical option key to resolved value.
        """
        from deepagents_code.config_manifest import get_config_options

        return self.resolve_options(get_config_options())

    # [해설] 모든 provider reload(generation 전진). 원격 managed fetch가 lock 안에서 일어날 수 있어 UI 경로에서는 비권장.
    def reload(self) -> None:
        """Propagate a source refresh to every provider.

        Advances the generation, so this also re-arms the source-level
        diagnostics -- see `reload_with_replacements`, which does the work.

        Reloads every provider *under this resolver's lock*, and a managed
        provider's loader can fetch over the network for the whole remote
        timeout. Every concurrent `get` and `resolve_options` blocks for that
        duration, which on the Textual event loop stalls the UI. Callers that
        may run against a remote descriptor should fetch first and hand the
        snapshot over -- `get_config_resolver(refresh_managed=True)` does
        exactly that -- rather than calling this.
        """
        self.reload_with_replacements({})

    # [해설] 미리 새로 고친 provider로 특정 rank를 교체하고 나머지는 reload한다. 호출자: get_config_resolver(refresh_managed=True).
    def reload_with_replacements(
        self,
        replacements: Mapping[int, ConfigProvider],
    ) -> None:
        """Refresh providers after installing already-refreshed replacements.

        Non-replaced providers are reloaded under this resolver's lock, so the
        caveat on `reload` about a managed loader's network I/O applies to any
        call that leaves the managed rank unreplaced.

        Args:
            replacements: Providers already bound to the desired generation,
                keyed by the rank they replace. Replacements are installed but
                not reloaded, so each must already hold a usable snapshot.

        Raises:
            ValueError: If a replacement rank is not in this resolver, or a
                replacement is not serving a usable snapshot.
        """
        with self._lock:
            # [해설][흐름] 1) 체인에 없는 rank 교체 요청은 거부.
            ranks = {provider.rank for provider in self._providers}
            unknown = replacements.keys() - ranks
            if unknown:
                msg = f"cannot replace unknown provider ranks: {sorted(unknown)}"
                raise ValueError(msg)
            # [해설][흐름] 2) 교체 provider가 사용할 수 없는 스냅샷을 서빙하면 거부 — 설치하면 그 계층의 제한(정책)이 사라져 fail-open이 된다.
            unusable = sorted(
                rank
                for rank, provider in replacements.items()
                if not _serves_usable_policy(provider)
            )
            if unusable:
                msg = (
                    f"replacement providers at ranks {unusable} are unusable; "
                    "installing one would drop the source's restrictions"
                )
                raise ValueError(msg)
            # [해설][흐름] 3) 교체 설치(순서 유지) → 4) 교체되지 않은 provider만 reload(파일 재파싱 등).
            self._providers = tuple(
                replacements.get(provider.rank, provider)
                for provider in self._providers
            )
            for provider in self._providers:
                if provider.rank not in replacements:
                    provider.reload()

        # [해설][흐름] 5) generation이 바뀌었으므로 소스 수준 진단 중복 제거 집합을 리셋(config_manifest.reset_source_diagnostics).
        # The source-level diagnostics (`shadowed`, `unusable`, `retained`) are
        # deduplicated so one `dcode config` sweep over ~200 options does not
        # print the same rejection 200 times. Scoping that to the process made
        # the second `/reload` of a still-broken file silent -- the reason
        # string is identical, so the user gets no message during exactly the
        # edit-and-retry loop where they need one. A generation advance ends
        # the sweep the dedup exists for, so it is the right scope.
        from deepagents_code.config_manifest import reset_source_diagnostics

        reset_source_diagnostics()

    # [해설] rank → ProviderStatus(이름·경로·사용 가능 여부 등) 스냅샷. CLI_RANK 설치 여부 확인에도 쓰인다.
    def provider_statuses(self) -> Mapping[int, ProviderStatus]:
        """Return immutable provider health keyed by precedence rank."""
        with self._lock:
            statuses = {
                provider.rank: provider.status() for provider in self._providers
            }
        return MappingProxyType(statuses)

    # [해설] 살아 있는 체인에 provider를 rank 순서로 끼워 넣는다. generation 전진·진단 리셋 없음(CLI/RELOAD 계층용).
    def install_provider(self, provider: ConfigProvider) -> None:
        """Insert a provider into the live chain, keeping rank order.

        Used for the CLI tier, which exists only after `argparse` runs — long
        after this resolver may have been built and cached. Unlike
        `reload_with_replacements`, this advances no generation and touches no
        files: the CLI provider is in-memory, so there is nothing to reload,
        and re-arming source diagnostics for an install that invalidates no
        snapshot would only risk a duplicate warning on the next resolution.

        Args:
            provider: Provider to insert. Its rank must not already be present.

        Raises:
            ValueError: If a provider already serves the new provider's rank.
        """
        with self._lock:
            ranks = {existing.rank for existing in self._providers}
            if provider.rank in ranks:
                msg = f"a provider already serves rank {provider.rank}"
                raise ValueError(msg)
            self._providers = tuple(
                sorted((*self._providers, provider), key=lambda p: p.rank)
            )

    # [해설] 해당 rank가 TomlFileProvider면 현재 스냅샷을 돌려준다. 같은 파일 generation으로 일회성 resolver를 만들 때 사용.
    def toml_snapshot(self, rank: int) -> TomlSnapshot | None:
        """Return the cached TOML snapshot at `rank`, if that provider is one.

        Lets a caller build a one-off resolver against the same file
        generation this resolver is serving -- for example, re-resolving an
        option with the managed tier masked while keeping the shared user
        snapshot instead of re-parsing `config.toml` off disk.

        Propagates the `RuntimeError` `current_snapshot` raises when the
        provider at `rank` produced no snapshot.

        Args:
            rank: Precedence rank whose snapshot to return.

        Returns:
            The provider's current snapshot, or `None` when no TOML provider
            sits at `rank` (environment and default providers carry none).
        """
        from deepagents_code.configuration.providers import TomlFileProvider

        with self._lock:
            for provider in self._providers:
                if provider.rank == rank and isinstance(provider, TomlFileProvider):
                    return provider.current_snapshot()
        return None


# [해설] 스냅샷 한 세대로 표준 체인을 만든다. 호출자: get_config_resolver(캐시 미스), 그리고 config.py·configuration/service.py·
# [해설] theme.py·update_check.py·integrations/sandbox_config.py·client/commands/config.py 등의 일회성 resolver.
# [해설][주의] keyword-only는 보안 장치다: managed/user 스냅샷 자리를 바꾸면 사용자 파일이 관리자 권한 rank를 얻는다.
def resolver_from_snapshots(
    *,
    managed: TomlSnapshot,
    user: TomlSnapshot,
    managed_loader: Callable[[], TomlSnapshot] | None = None,
    user_loader: Callable[[], TomlSnapshot] | None = None,
    cli_provider: ConfigProvider | None = None,
) -> ConfigResolver:
    """Build the standard provider chain from one file-snapshot generation.

    Keyword-only by design. The two snapshots share a type, so a positional
    transposition would load the user's writable `config.toml` at
    `MANAGED_RANK` -- user data acquiring managed precedence, which is the one
    escalation this trust boundary exists to prevent. Nothing downstream
    rejects it: this function never inspects `managed.status.name`, and
    `TomlFileProvider` accepts whatever rank it is handed.

    Args:
        managed: Managed TOML snapshot.
        user: User TOML snapshot.
        managed_loader: Optional managed reload operation.
        user_loader: Optional user reload operation.
        cli_provider: Optional parsed-argument provider for this process.

    Returns:
        Resolver containing managed, environment, user, and default providers,
            plus the CLI provider when one is supplied.
    """
    from deepagents_code.configuration.providers import (
        DefaultProvider,
        EnvProvider,
        TomlFileProvider,
    )

    # [해설][흐름] 1) 경로 없는(메모리) 스냅샷은 path=None 그대로 → reload 시 cwd의 엉뚱한 파일을 정책으로 읽지 않게.
    # Snapshots built in-memory carry no path. Pass that through rather than
    # inventing a relative filename: `TomlFileProvider.load` would resolve it
    # against the process working directory, so a later `reload()` on a
    # diagnostic resolver would read whatever `./managed_config.toml` happens
    # to sit in the repo the agent is running in and treat it as policy.
    managed_path = managed.status.path
    user_path = user.status.path
    # [해설][흐름] 2) 체인: managed(200) → [cli(300)] → env(400) → user(500) → default(1000). RELOAD(350)는 여기서 만들지 않는다.
    providers: tuple[ConfigProvider, ...] = (
        TomlFileProvider(
            name=managed.status.name,
            path=managed_path,
            rank=MANAGED_RANK,
            durable=True,
            snapshot=managed,
            loader=managed_loader,
        ),
        *((cli_provider,) if cli_provider is not None else ()),
        EnvProvider(),
        TomlFileProvider(
            name=user.status.name,
            path=user_path,
            rank=USER_RANK,
            durable=True,
            snapshot=user,
            loader=user_loader,
        ),
        DefaultProvider(),
    )
    return ConfigResolver(providers)


# [해설] 캐시 키: (사용자 config 경로, managed 경로). 경로가 바뀌면(테스트·managed 설치/제거) resolver를 재구성한다.
@dataclass(frozen=True, slots=True)
class _ResolverKey:
    """The pair of file paths a shared resolver is built for.

    Named rather than a bare `tuple[object, ...]`: a key built with different
    arity or field order compares unequal, silently rebuilds the resolver, and
    loses the single-generation guarantee with no error anywhere.
    """

    user_path: Path
    managed_path: Path | None


# [해설] 프로세스 캐시 상태: (키, resolver) 한 쌍과 설치된 CLI provider.
@dataclass(slots=True)
class _ResolverCache:
    """Mutable process resolver cache guarded by one lifecycle lock.

    One field, not a key and a resolver side by side: those admit a populated
    key with no resolver, and a lookup that trusts either half alone would then
    read a stale generation or rebuild one that already exists.
    """

    entry: tuple[_ResolverKey, ConfigResolver] | None = None
    cli_provider: ConfigProvider | None = None


# [해설] 모듈 전역 캐시와 그 lock. get_config_resolver/install_cli_provider/reset_config_resolver가 공유.
_resolver_cache_lock = threading.RLock()
_resolver_cache = _ResolverCache()


# [해설] 설치된 CLI provider 조회. 일회성 resolver를 만드는 호출자가 CLI 계층을 빠뜨리지 않게 한다.
def installed_cli_provider() -> ConfigProvider | None:
    """Return the parsed-argument provider installed for this process.

    Ad-hoc resolvers built from caller-supplied snapshots do not go through the
    process cache, so they have no CLI tier unless they ask for this one. A
    reader that omits it reports the wrong source for any option a flag in the
    current argv is setting.

    Returns:
        The installed provider, or `None` before `install_cli_provider` runs.
    """
    with _resolver_cache_lock:
        return _resolver_cache.cli_provider


# [해설] managed 스냅샷을 새로 가져오되(refresh=True), 가져온 것이 사용 가능한데 정책 위반(managed_policy_violations)이면
# [해설] 캐시된 기존 스냅샷(get_managed_snapshot())을 돌려준다(추정: 강제 불가능한 새 정책 대신 이전 세대 유지).
def _reload_enforceable_managed_snapshot() -> TomlSnapshot:
    """Return a refreshed managed snapshot only when policy can enforce it."""
    from deepagents_code.configuration.service import (
        get_managed_snapshot,
        managed_policy_violations,
    )

    candidate = get_managed_snapshot(refresh=True)
    if candidate.status.usable and managed_policy_violations(
        candidate.data,
        status=candidate.status,
    ):
        return get_managed_snapshot()
    return candidate


# [해설] 프로세스 공유 resolver를 반환(없으면 생성). 거의 모든 설정 읽기의 시작점.
def get_config_resolver(
    *,
    refresh_managed: bool = False,
    managed_snapshot: TomlSnapshot | None = None,
    cli_provider: ConfigProvider | None = None,
) -> ConfigResolver:
    """Return the shared process resolver for the active config paths.

    Args:
        refresh_managed: Refresh the user and environment tiers on an existing
            matching resolver. The managed tier is not re-read: it is replaced
            with the snapshot the caller already validated, so one reload
            observes one managed-file generation.
        managed_snapshot: Already refreshed and validated managed snapshot.
            Supplies the cache key, and builds the resolver when the cache
            misses. On a cache hit it is installed only when `refresh_managed`
            is set -- without it the resolver keeps the generation it is
            already serving, so the snapshot must be that same generation.
        cli_provider: Parsed-argument provider to install for this process.

    Returns:
        Resolver shared by consumers of the active managed and user paths.

    Raises:
        ValueError: If `managed_snapshot` is a different generation than the
            one already installed and `refresh_managed` is not set. The
            snapshot would otherwise be discarded in silence. Also if
            `cli_provider` differs from the one already installed for this
            process: one argv yields one CLI tier, and silently keeping either
            provider would misreport every flag the other one carries.
    """
    from deepagents_code.configuration.providers import TomlFileProvider
    from deepagents_code.configuration.service import get_managed_snapshot
    from deepagents_code.model_config import DEFAULT_CONFIG_PATH

    # [해설][흐름] 1) managed 스냅샷 결정: 호출자가 준 것 > refresh 요청 시 새로 fetch(lock 밖에서, 네트워크 가능) > 캐시.
    if managed_snapshot is not None:
        managed = managed_snapshot
    elif refresh_managed:
        managed = _reload_enforceable_managed_snapshot()
    else:
        managed = get_managed_snapshot()
    # [해설][흐름] 2) 캐시 키 계산(DEFAULT_CONFIG_PATH, managed 경로).
    key = _ResolverKey(DEFAULT_CONFIG_PATH, managed.status.path)
    with _resolver_cache_lock:
        # [해설][흐름] 3) CLI provider 일관성: 이미 다른 CLI provider가 있으면 거부(argv 하나당 CLI 계층 하나).
        installed_cli = _resolver_cache.cli_provider
        if cli_provider is not None:
            if installed_cli is not None and installed_cli != cli_provider:
                msg = "a different CLI provider is already installed for this process"
                raise ValueError(msg)
            _resolver_cache.cli_provider = cli_provider
            installed_cli = cli_provider
        # [해설][흐름] 4) 캐시 미스/키 변경/CLI 계층 누락이면 사용자 config.toml을 읽어 새 체인을 만들고 진단을 리셋.
        entry = _resolver_cache.entry
        if (
            entry is None
            or entry[0] != key
            or (
                cli_provider is not None
                and CLI_RANK not in entry[1].provider_statuses()
            )
        ):
            user_provider = TomlFileProvider(
                name="config.toml", path=DEFAULT_CONFIG_PATH
            )
            user = user_provider.load()
            resolver = resolver_from_snapshots(
                managed=managed,
                user=user,
                managed_loader=_reload_enforceable_managed_snapshot,
                user_loader=user_provider.load,
                cli_provider=installed_cli,
            )
            _resolver_cache.entry = (key, resolver)
            # A rebuild is a generation advance too: the key changes when
            # managed policy is installed or removed, and the dedup set would
            # otherwise carry rejections from the generation just replaced.
            from deepagents_code.config_manifest import reset_source_diagnostics

            reset_source_diagnostics()
            return resolver
        # [해설][흐름] 5) 캐시 적중.
        resolver = entry[1]
        # [해설][흐름] 6) refresh 요청이 없으면 그대로 반환. 단 호출자가 준 managed 스냅샷이 현재 세대와 다르면 조용히 버리지 않고 ValueError.
        if not refresh_managed:
            # A caller that hands over a snapshot and does not ask for a
            # refresh is telling the resolver to keep serving what it has, so
            # the two must already agree. They do today -- the preview path
            # takes its snapshot with `refresh=False`, which returns the
            # cached one -- but nothing in the signature says so, and the
            # alternative is discarding a validated generation in silence.
            if managed_snapshot is not None:
                installed = resolver.toml_snapshot(MANAGED_RANK)
                if installed is not None and installed != managed_snapshot:
                    msg = (
                        "managed_snapshot is a different generation than the "
                        "one in force; pass refresh_managed=True to install it"
                    )
                    raise ValueError(msg)
            return resolver
        # [해설][흐름] 7) refresh: managed rank만 미리 받은 스냅샷으로 교체하고 나머지(env/user)는 reload.
        resolver.reload_with_replacements(
            {
                MANAGED_RANK: _managed_replacement_provider(
                    resolver,
                    managed,
                )
            }
        )
        return resolver


# [해설] 교체 provider가 신뢰할 수 있는 스냅샷을 서빙하는지. 최신 상태가 unusable이어도 이전 usable 스냅샷을 유지 중이면 허용.
def _serves_usable_policy(provider: ConfigProvider) -> bool:
    """Whether a replacement is serving a snapshot resolution can trust.

    A replacement bypasses `reload`, so it needs a usable generation behind it.
    Its latest status may still be unusable while it safely retains the
    previous snapshot; rejecting that state would erase the failed-refresh
    diagnostic. A provider whose *served* snapshot is unusable would resolve as
    "this source declares nothing" and silently let lower ranks win.

    Args:
        provider: Replacement about to be installed.

    Returns:
        Whether the generation this provider resolves from is usable.
    """
    from deepagents_code.configuration.providers import TomlFileProvider

    if provider.status().usable:
        return True
    return (
        isinstance(provider, TomlFileProvider)
        and provider.current_snapshot().status.usable
    )


# [해설] managed rank 교체용 provider 생성. 설치된 스냅샷을 기반으로 두고 후보 스냅샷을 reload_from_snapshot으로 반영한다
# [해설] (추정: 후보가 실패 상태면 이전 세대를 유지하면서 실패 진단만 기록).
def _managed_replacement_provider(
    resolver: ConfigResolver,
    candidate: TomlSnapshot,
) -> ConfigProvider:
    """Build a current managed replacement that retains failed refreshes safely.

    Args:
        resolver: Resolver currently serving the previous generation.
        candidate: Snapshot fetched before taking the resolver lock. A newer
            enforceable generation may have published while this caller waited.

    Returns:
        Replacement carrying the current enforceable generation or the
        candidate's failed-refresh status.

    Raises:
        RuntimeError: If the shared resolver has no managed TOML provider.
    """
    from deepagents_code.configuration.providers import TomlFileProvider
    from deepagents_code.configuration.service import get_managed_snapshot

    installed = resolver.toml_snapshot(MANAGED_RANK)
    if installed is None:
        msg = "shared config resolver has no managed TOML provider"
        raise RuntimeError(msg)
    # [해설] 후보가 usable이면 lock 대기 중 더 새 세대가 게시됐을 수 있으므로 현재 캐시 스냅샷을 다시 읽는다.
    if candidate.status.usable:
        candidate = get_managed_snapshot()
    replacement = TomlFileProvider(
        name=candidate.status.name,
        path=candidate.status.path,
        rank=MANAGED_RANK,
        durable=True,
        snapshot=installed,
        loader=_reload_enforceable_managed_snapshot,
    )
    replacement.reload_from_snapshot(candidate)
    return replacement


# [해설] argparse 직후 main.py가 호출. config 파일을 읽지 않으므로 `--help` 같은 빠른 경로의 시작 성능을 해치지 않는다.
# [해설] 캐시된 resolver가 있으면 즉시 체인에 삽입, 없으면 보관했다가 첫 get_config_resolver가 사용.
def install_cli_provider(cli_provider: ConfigProvider) -> None:
    """Install the process CLI provider without touching config files.

    Unlike `get_config_resolver(cli_provider=...)`, this never imports
    `deepagents_code.model_config` and never reads a TOML snapshot: it either
    attaches the provider to the already-cached resolver or stashes it for the
    first real `get_config_resolver` call to pick up. The startup fast paths
    (`--help`, bare command groups) parse arguments and return before any
    config resolution happens, so paying the settings-bootstrap import cost
    here would break the startup-perf contract those paths are tested
    against.

    Args:
        cli_provider: Parsed-argument provider to install for this process.

    Raises:
        ValueError: If a different CLI provider is already installed.
    """
    with _resolver_cache_lock:
        installed = _resolver_cache.cli_provider
        if installed is not None and installed != cli_provider:
            msg = "a different CLI provider is already installed for this process"
            raise ValueError(msg)
        _resolver_cache.cli_provider = cli_provider
        entry = _resolver_cache.entry
        if entry is not None and CLI_RANK not in entry[1].provider_statuses():
            entry[1].install_provider(cli_provider)


# [해설] 테스트 전용: 캐시 resolver와 CLI provider 제거.
def reset_config_resolver() -> None:
    """Drop the cached process resolver.

    Test-only, and paired with `service.invalidate_config_sources`: the two
    caches are keyed differently, so clearing only the managed snapshot leaves
    this one serving the previous test's generation. Tests escaped that today
    only by incidentally monkeypatching `DEFAULT_CONFIG_PATH`, which changes
    the key; one that exercises the resolver at an unchanged path would inherit
    stale state.
    """
    # No `reset_source_diagnostics` here: dropping the entry makes the next
    # `get_config_resolver` take the cache-miss branch, which re-arms them as
    # part of building the new generation. Importing the manifest from this
    # teardown path also breaks the test that stubs it out of `sys.modules`.
    with _resolver_cache_lock:
        _resolver_cache.entry = None
        _resolver_cache.cli_provider = None


# [해설] rank 기반 해석의 핵심 함수. ConfigResolver._resolve 외에 mcp_disabled.py·model_config.py가 직접 호출하기도 한다.
def resolve_ranked[T](
    providers: Sequence[RankedProviderValue[T]],
    *,
    strategy: str = "replace",
) -> ResolvedValue[T] | None:
    """Resolve provider results by numeric rank and per-option merge strategy.

    Lower ranks win. For replacement options, a `Found` from a durable tier
    masks lower-precedence non-durable tiers. The mask is intentionally
    directional: a persisted user value at rank 500 cannot retroactively hide
    a higher-precedence environment value at rank 400.

    Accumulating strategies combine tiers by definition, so they retain every
    valid contribution. This preserves the existing fail-closed deny-list
    unions and deep TOML composition; treating accumulation as replacement
    would silently discard restrictions or sibling table leaves.

    Args:
        providers: Already-coerced provider results. Ranks must be unique.
        strategy: `replace`, `union`, or `deep_merge`.

    Returns:
        A resolved value, or `None` when no provider returned `Found`.

    Raises:
        ValueError: If ranks repeat or `strategy` is unknown.
    """
    # [해설][흐름] 1) rank 중복·알 수 없는 전략 검증.
    ordered = sorted(providers, key=lambda provider: provider.rank)
    ranks = [provider.rank for provider in ordered]
    if len(set(ranks)) != len(ranks):
        msg = "ranked config providers must have unique ranks"
        raise ValueError(msg)
    if strategy not in {"replace", "union", "deep_merge"}:
        msg = f"unknown config merge strategy: {strategy}"
        raise ValueError(msg)

    # [해설][흐름] 2) rank별 건강/상태/진단 표를 불변 매핑으로 만든다(Found 여부와 무관하게 모든 계층 포함).
    tier_health = MappingProxyType(
        {provider.rank: provider.result for provider in ordered}
    )
    provider_status = MappingProxyType(
        {provider.rank: provider.status for provider in ordered}
    )
    tier_diagnostics = MappingProxyType(
        {provider.rank: provider.diagnostics for provider in ordered}
    )
    # [해설][흐름] 3) Found 계층만 추림. 없으면 None(호출자가 기본값 폴백).
    found = [provider for provider in ordered if isinstance(provider.result, Found)]
    if not found:
        return None
    # [해설][흐름] 4) 누적 전략은 전용 함수로.
    if strategy == "union":
        return _resolve_ranked_union(
            found,
            tier_health,
            provider_status,
            tier_diagnostics,
        )
    if strategy == "deep_merge":
        return _resolve_ranked_deep_merge(
            found,
            tier_health,
            provider_status,
            tier_diagnostics,
        )

    # [해설][흐름] 5) replace: 더 강한(작은 rank) durable Found가 있는 non-durable Found 계층을 masked로 표시.
    # [해설] 정렬상 winner는 항상 가장 작은 rank의 Found이므로(그보다 작은 durable은 없음) masked는 승자를 바꾸지 않고,
    # [해설] "이 env 값은 관리 정책에 가려짐" 같은 출처/진단 표시용 정보가 된다.
    durable_ranks = tuple(provider.rank for provider in found if provider.durable)
    masked = frozenset(
        provider.rank
        for provider in found
        if not provider.durable
        and any(durable_rank < provider.rank for durable_rank in durable_ranks)
    )
    winner = next(provider for provider in found if provider.rank not in masked)
    return ResolvedValue(
        _provider_value(winner),
        MappingProxyType({winner.rank: frozenset({()})}),
        tier_health,
        provider_status,
        masked,
        (winner.rank,),
        tier_diagnostics,
    )


# [해설] 누적이 불가능할 때(목록/테이블이 아닌 값) 가장 강한 계층 값으로 대체. managed 스냅샷 별칭 오염을 막으려 deepcopy.
def _replace_with_strongest[T](
    found: Sequence[RankedProviderValue[T]],
    tier_health: Mapping[int, ProviderResult[T]],
    provider_status: Mapping[int, ProviderStatus],
    tier_diagnostics: Mapping[int, tuple[str, ...]],
) -> ResolvedValue[T]:
    """Resolve to the strongest-precedence provider when accumulation fails.

    The value is copied. Provider values alias the process-wide managed
    snapshot, so handing out a live reference would let a consumer mutate
    administrator policy for the rest of the session.

    Returns:
        The lowest-rank provider's value, deep-copied.
    """
    winner = found[0]
    return ResolvedValue(
        deepcopy(_provider_value(winner)),
        MappingProxyType({winner.rank: frozenset({()})}),
        tier_health,
        provider_status,
        selected_ranks=(winner.rank,),
        tier_diagnostics=tier_diagnostics,
    )


# [해설] union 전략: deny-list 같은 목록을 약한→강한 순으로 합친다(중복 제거, 순서 유지). 모든 기여 rank가 provenance에 남는다.
def _resolve_ranked_union[T](
    found: Sequence[RankedProviderValue[T]],
    tier_health: Mapping[int, ProviderResult[T]],
    provider_status: Mapping[int, ProviderStatus],
    tier_diagnostics: Mapping[int, tuple[str, ...]],
) -> ResolvedValue[T]:
    """Accumulate list-like providers from weakest to strongest rank.

    Returns:
        The union, or the strongest-precedence (lowest-rank) replacement when a
        value is not list-like.
    """
    # [해설] 각 계층 값을 목록으로 정규화(쉼표 문자열도 허용). 하나라도 목록이 아니면 가장 강한 계층으로 대체.
    entries = [union_entries(_provider_value(provider)) for provider in found]
    if any(value is None for value in entries):
        return _replace_with_strongest(
            found, tier_health, provider_status, tier_diagnostics
        )
    union: list[Any] = []
    # [해설] found는 강한 순이므로 reversed로 약한 계층부터 누적한다.
    for value in reversed(entries):
        union = union_lists(union, cast("list[Any]", value))
    provenance = MappingProxyType(
        {provider.rank: frozenset({()}) for provider in found}
    )
    return ResolvedValue(
        cast("T", union),
        provenance,
        tier_health,
        provider_status,
        selected_ranks=tuple(provider.rank for provider in found),
        tier_diagnostics=tier_diagnostics,
    )


# [해설] deep_merge 전략: 테이블을 약한→강한 순으로 깊은 병합하고, leaf 경로별로 기여 rank를 기록한다.
def _resolve_ranked_deep_merge[T](
    found: Sequence[RankedProviderValue[T]],
    tier_health: Mapping[int, ProviderResult[T]],
    provider_status: Mapping[int, ProviderStatus],
    tier_diagnostics: Mapping[int, tuple[str, ...]],
) -> ResolvedValue[T]:
    """Deep-merge mapping providers from weakest to strongest rank.

    A tier that does not hold a mapping cannot be merged. Such a tier falls
    back to replacement by the strongest-precedence (lowest-rank) provider,
    matching `_resolve_ranked_union`. Returning the non-mapping tier itself
    would let a weaker tier displace managed policy.

    Returns:
        The merged mapping, or the strongest provider's value when any tier
        cannot be merged.
    """
    # [해설][흐름] 1) 가장 약한 계층을 바탕으로 시작(dict가 아니면 가장 강한 계층으로 대체).
    weakest = found[-1]
    value = _provider_value(weakest)
    if not isinstance(value, dict):
        return _replace_with_strongest(
            found, tier_health, provider_status, tier_diagnostics
        )
    merged = deepcopy(cast("dict[str, Any]", value))
    leaves = _ranked_leaf_provenance(merged, weakest.rank)
    # [해설][흐름] 2) 더 강한 계층을 차례로 덮어 병합. 중간에 dict가 아닌 계층이 있으면 전체를 가장 강한 계층 값으로 대체.
    for provider in reversed(found[:-1]):
        higher = _provider_value(provider)
        if not isinstance(higher, dict):
            return _replace_with_strongest(
                found, tier_health, provider_status, tier_diagnostics
            )
        merged, leaves = _merge_ranked_tables(
            merged,
            cast("dict[str, Any]", higher),
            leaves,
            provider.rank,
        )
    # [해설][흐름] 3) leaf→rank 기록을 rank→leaf 집합으로 뒤집어 provenance 생성.
    grouped: dict[int, set[tuple[str, ...]]] = {}
    for path, rank in leaves.items():
        grouped.setdefault(rank, set()).add(path)
    provenance = MappingProxyType(
        {rank: frozenset(paths) for rank, paths in grouped.items()}
    )
    return ResolvedValue(
        cast("T", merged),
        provenance,
        tier_health,
        provider_status,
        selected_ranks=tuple(provider.rank for provider in found),
        tier_diagnostics=tier_diagnostics,
    )


# [해설] 두 dict를 재귀 병합하며 (경로 튜플 → rank) 출처를 갱신한다. 교체된 경로 아래의 옛 leaf 출처는 삭제.
def _merge_ranked_tables(
    lower: dict[str, Any],
    higher: dict[str, Any],
    provenance: dict[tuple[str, ...], int],
    higher_rank: int,
    *,
    prefix: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[tuple[str, ...], int]]:
    """Deep-merge two mappings while retaining tuple-path rank provenance.

    Returns:
        The merged table and tuple-path-to-rank provenance.
    """
    merged = deepcopy(lower)
    ranked = dict(provenance)
    for key, value in higher.items():
        path = (*prefix, key)
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key], ranked = _merge_ranked_tables(
                cast("dict[str, Any]", existing),
                cast("dict[str, Any]", value),
                ranked,
                higher_rank,
                prefix=path,
            )
            continue
        merged[key] = deepcopy(value)
        for leaf in tuple(ranked):
            if leaf[: len(path)] == path:
                ranked.pop(leaf)
        ranked.update(_ranked_leaf_provenance(value, higher_rank, path))
    return merged, ranked


# [해설] 값 아래 모든 leaf 경로를 rank에 귀속. 빈 dict는 (루트가 아니면) 그 자체를 leaf로 본다.
def _ranked_leaf_provenance(
    value: object, rank: int, path: tuple[str, ...] = ()
) -> dict[tuple[str, ...], int]:
    """Attribute every leaf under `value` to a numeric provider rank.

    Returns:
        Tuple-path-to-rank provenance for every leaf.
    """
    if isinstance(value, dict):
        if not value:
            return {path: rank} if path else {}
        result: dict[tuple[str, ...], int] = {}
        for key, child in cast("dict[str, object]", value).items():
            result.update(_ranked_leaf_provenance(child, rank, (*path, key)))
        return result
    return {path: rank}


# [해설] Found 값을 꺼내는 내부 헬퍼. 누적 해석은 Found만 받으므로 아니면 RuntimeError(내부 버그 신호).
def _provider_value[T](provider: RankedProviderValue[T]) -> T:
    """Narrow a provider known by the resolver to hold `Found`.

    Returns:
        The provider's coerced value.

    Raises:
        RuntimeError: If an internal accumulating resolver receives a non-found tier.
    """
    result = provider.result
    if isinstance(result, Found):
        return cast("T", result.value)
    msg = f"rank {provider.rank} did not contain a found value"
    raise RuntimeError(msg)


# [해설] deny-list 두 계층 합치기: lower 순서 유지 + higher의 새 항목 추가. resolver와 merge_toml_tables(_merge)가 공유.
def union_lists(lower: list[Any], higher: list[Any]) -> list[Any]:
    """Accumulate two deny-list layers, keeping order and dropping duplicates.

    Shared with the merger so a deny list cannot union in one reader and
    replace in another.

    Returns:
        The lower list followed by the higher entries it does not already hold.
    """
    union = deepcopy(lower)
    for item in higher:
        if item not in union:
            union.append(deepcopy(item))
    return union


# [해설] deny-list 값 정규화: 쉼표 문자열 → 목록, 목록은 그대로, 그 외는 None(목록 불가).
def union_entries(value: object) -> list[Any] | None:
    """Normalize one deny-list layer to its entries.

    A deny list may be written as a TOML array or as a comma-separated string
    (`disabled_servers = "a, b"`), and the runtime readers treat the two as
    equivalent — `mcp_disabled._strict_entries` and `model_config._toml_str_list`
    both split on commas. The merge has to accept both spellings too. It did
    not, so a managed string layer was dropped in favor of the user's array and
    the provenance then credited the user's file for a leaf managed policy
    contributes to.

    Returns:
        The trimmed entries, or `None` when the value cannot hold entries.
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return value
    return None


# [해설] TOML 테이블 깊은 병합 + 점 표기 leaf 출처 맵. 주 사용처: configuration/service.py의 managed-over-user 병합,
# [해설] config_manifest.py, client/commands/config.py, main.py. (rank 숫자 대신 사람이 읽는 source 라벨을 쓴다.)
def merge_toml_tables(
    lower: Mapping[str, Any],
    higher: Mapping[str, Any],
    *,
    lower_source: str,
    higher_source: str,
    union_paths: frozenset[tuple[str, ...]] = frozenset(),
    higher_leaf_is_valid: Callable[[tuple[str, ...], object], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Deep-merge TOML tables with higher-precedence leaf provenance.

    Args:
        lower: Lower-precedence table.
        higher: Higher-precedence table, whose leaves win.
        lower_source: Source label recorded for surviving `lower` leaves.
        higher_source: Source label recorded for surviving `higher` leaves.
        union_paths: Paths whose lists accumulate instead of being replaced.
            Deny lists must union, because replacing one would be a fail-open.
            Paths match relative to the tables passed here, so a merge of one
            subtree needs them rebased (see `service.union_paths_under`).
        higher_leaf_is_valid: Optional check applied to a `higher` value before
            it displaces a `lower` one. Return `False` to keep the lower value,
            which stops a wrong-typed higher value from discarding a valid
            lower subtree. Receives paths on the same relative basis as
            `union_paths`. Every managed merge passes one, by way of
            `service.merge_managed_over_user`; omitting it leaves the merger
            with no type information, so it displaces only a table that holds
            no nested table.

    Returns:
        Merged table and dotted leaf-to-source mapping.
    """
    merged, provenance = _merge(
        lower,
        higher,
        lower_source=lower_source,
        higher_source=higher_source,
        union_paths=union_paths,
        higher_leaf_is_valid=higher_leaf_is_valid,
    )
    return merged, _dotted(_drop_ancestor_entries(provenance))


# [해설] 튜플 경로를 표시용 "a.b.c"로 변환. 내부에서는 따옴표 키의 점 모호성 때문에 끝까지 튜플로 유지한다.
def _dotted(provenance: dict[tuple[str, ...], str]) -> dict[str, str]:
    """Join tuple paths for display.

    Provenance is keyed by path tuple everywhere inside this module. TOML allows
    a quoted key that contains dots (`"a.b" = 1` parses to the single key
    `a.b`), so a dotted string is a lossy key: it made `_drop_ancestor_entries`
    delete a live sibling leaf named `a`, and credited the wrong tier for the
    flat key. Joining happens once, here, where the ambiguity is only cosmetic.

    Returns:
        Provenance keyed by dotted path.
    """
    return {".".join(path): source for path, source in provenance.items()}


# [해설] 다른 항목의 조상 경로인 항목 제거 → provenance에는 leaf만 남는다.
def _drop_ancestor_entries(
    provenance: dict[tuple[str, ...], str],
) -> dict[tuple[str, ...], str]:
    """Remove entries that are a strict ancestor of another entry.

    A lower empty table that the higher table fills leaves an entry for the
    table itself: it enters the recursion through `lower_provenance`, which
    carries the parent's own path, and the level that fills it never removes it.
    The result claimed a table was a user-controlled leaf alongside the managed
    leaves inside it. A path cannot be both a leaf and a parent, so the ancestor
    is always the stale one.

    Returns:
        Provenance with only leaf entries.
    """
    keys = tuple(provenance)
    return {
        path: source
        for path, source in provenance.items()
        if not any(other[: len(path)] == path and other != path for other in keys)
    }


# [해설] merge_toml_tables의 재귀 본체. 보안 규칙(모양 충돌·검증기·deny-list union)을 경로마다 적용한다.
def _merge(
    lower: Mapping[str, Any],
    higher: Mapping[str, Any],
    *,
    lower_source: str,
    higher_source: str,
    union_paths: frozenset[tuple[str, ...]],
    higher_leaf_is_valid: Callable[[tuple[str, ...], object], bool] | None,
    lower_provenance: dict[tuple[str, ...], str] | None = None,
    path_prefix: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[tuple[str, ...], str]]:
    """Recursive half of `merge_toml_tables`.

    Separate so the public signature carries no parameter a caller must not
    pass: `lower_provenance` has to arrive already scoped to `path_prefix`, and
    an unscoped mapping produces wrong provenance with no error.

    Returns:
        Merged table and path-keyed leaf-to-source mapping.
    """
    # [해설][흐름] 1) lower를 복사하고, 이번 스코프의 lower 출처(재귀 시 부모가 넘김)를 준비.
    merged: dict[str, Any] = deepcopy(dict(lower))
    provenance = dict(
        lower_provenance or _leaf_provenance(lower, lower_source, path_prefix)
    )
    # [해설][흐름] 2) higher의 각 키를 처리. 아래 a)~e) 순서로 분기한다.
    for key, value in higher.items():
        path = (*path_prefix, key)
        existing = merged.get(key)
        # [해설][흐름] a) 모양 충돌(lower=table, higher=scalar) + 검증기 없음: 중첩 테이블을 가진 lower는 보존, 스칼라만 가진 테이블은 교체.
        # A higher scalar must replace a lower table, whatever the table holds.
        # Keeping the table lets a shape collision defeat the higher value.
        # Typed readers then reject the table and use the built-in default.
        #   Example: a user `[threads.relative_time]` table against a managed
        #   `relative_time = false`.
        # Depth is not consulted, so deeper nesting cannot restore the bypass.
        # With `higher_leaf_is_valid`, the check below gates the replacement.
        # That keeps a wrong-typed higher scalar from discarding a valid lower
        # subtree. Without a validator there is no type information here, so
        # only a table that holds no nested table is displaced.
        if (
            isinstance(existing, dict)
            and not isinstance(value, dict)
            and higher_leaf_is_valid is None
            and not _overriding_table_is_scalar_only(existing)
        ):
            continue
        # [해설][흐름] b) 검증기가 higher 값을 거부하면 lower 유지(잘못된 타입의 managed 값이 유효한 사용자 값을 지우지 않게).
        # Validate every managed value at a manifest-backed scalar path,
        # including TOML tables. A table cannot be passed to the validator as
        # a leaf through the recursive branch below, so validating only
        # non-dicts would let `[models.default]` replace a valid string with a
        # dictionary that later runtime readers cannot use.
        if higher_leaf_is_valid is not None and not higher_leaf_is_valid(path, value):
            continue
        # [해설][흐름] c) deny-list 경로: 둘 다 목록이면 union, higher가 목록이 아니면 lower 유지(거부 목록이 사라지는 fail-open 방지).
        if path in union_paths:
            lower_entries = union_entries(existing)
            higher_entries = union_entries(value)
            if lower_entries is not None and higher_entries is None:
                # A higher value that cannot hold names must never replace a
                # deny list: that would drop the lower layer's denials.
                continue
            if lower_entries is not None and higher_entries is not None:
                merged[key] = union_lists(lower_entries, higher_entries)
                provenance[path] = _combined_source(lower_source, higher_source)
                continue
        # [해설][흐름] d) 둘 다 테이블이면 재귀 병합하고 해당 서브트리 출처를 재귀 결과로 교체.
        if isinstance(existing, dict) and isinstance(value, dict):
            nested, nested_provenance = _merge(
                existing,
                value,
                lower_source=lower_source,
                higher_source=higher_source,
                union_paths=union_paths,
                higher_leaf_is_valid=higher_leaf_is_valid,
                lower_provenance={
                    leaf: source
                    for leaf, source in provenance.items()
                    if leaf[: len(path)] == path
                },
                path_prefix=path,
            )
            merged[key] = nested
            # Drop this subtree's old leaves first. A nested merge can delete a
            # leaf (a higher scalar replacing a lower table), and keeping the
            # parent-scope entry would report a path that no longer exists as
            # user-controlled — in the output an administrator reads to audit
            # what policy enforces.
            for leaf in tuple(provenance):
                if leaf[: len(path)] == path:
                    provenance.pop(leaf)
            provenance.update(nested_provenance)
            continue
        # [해설][흐름] e) 그 외: higher 값으로 교체하고 그 경로 아래 옛 출처를 지운 뒤 higher 출처로 기록.
        merged[key] = deepcopy(value)
        for leaf in tuple(provenance):
            if leaf[: len(path)] == path:
                provenance.pop(leaf)
        provenance.update(_leaf_provenance(value, higher_source, path))
    return merged, provenance


# [해설] 테이블에 비어 있지 않은 하위 테이블이 없으면 True → _merge a)에서 higher 스칼라가 이 테이블을 대체할 수 있다.
def _overriding_table_is_scalar_only(table: dict[str, Any]) -> bool:
    """Return `True` when `table` holds no non-empty nested tables at any depth.

    Only direct children need checking: a nested table at any depth makes its
    own parent chain non-empty, so an empty direct child cannot hide one.
    Empty nested tables carry no lower values worth preserving, so they do not
    stop a higher-precedence scalar from replacing the table.
    """
    for child in cast("dict[str, object]", table).values():
        if isinstance(child, dict) and child:
            return False
    return True


# [해설] 값 아래 모든 leaf에 source 라벨을 매핑. 루트 빈 테이블은 leaf로 치지 않는다(감사 출력 오염 방지).
def _leaf_provenance(
    value: object, source: str, path: tuple[str, ...]
) -> dict[tuple[str, ...], str]:
    """Return provenance entries for every leaf under `value`."""
    if isinstance(value, dict):
        if not value:
            # An empty table at the root is not a leaf: it would key the whole
            # mapping. Every merge on a machine with no user `config.toml`
            # produced that entry, in the output an administrator reads to audit
            # what policy enforces.
            if not path:
                return {}
            return {path: source}
        result: dict[tuple[str, ...], str] = {}
        for key, child in cast("dict[str, object]", value).items():
            result.update(_leaf_provenance(child, source, (*path, key)))
        return result
    return {path: source}


# [해설] 서로 다른 라벨이면 "higher + lower"로 합쳐 union된 deny-list의 복합 출처를 표시.
def _combined_source(lower: str, higher: str) -> str:
    """Combine distinct source labels in precedence order.

    Returns:
        One source or a higher-plus-lower label.
    """
    if lower == higher:
        return higher
    return f"{higher} + {lower}"
