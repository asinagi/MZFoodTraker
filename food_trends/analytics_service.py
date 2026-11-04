"""B8 food analytics read models: read-time aggregation of B5 observations around a stable FOOD_ID.

Counting rules (docs/app-api/DATA_DICTIONARY.md):
  * a confirmed item is a source item with an ACTIVE, unexpired FOOD_MENTION on AVAILABLE evidence;
    ITEM_QUERY_MATCH search evidence is never counted, and every count is a set of distinct items, so an
    item found through several aliases, queries or runs counts once per food, source and bucket;
  * an item counts on each KST day a run of the included origin observed it; weeks and periods recompute
    the item set instead of adding days;
  * a day is observed only inside the source's observation scope (RSS: any successful feed run that day;
    Instagram: a successful run of one of the food's own queries, from the per-food observed_posts rows B5
    writes for every served food). Out of scope is NOT_OBSERVED, never zero; a day whose items lost their
    evidence (retention, deletion) is EVIDENCE_EXPIRED;
  * period values need every day observed and complete; growth needs both periods COMPLETE;
  * legacy_unknown observations are reported beside, never inside, the values; sources stay separate and
    no composite score exists.
Nothing is written: aggregate_invalidations are not consumed by read-time aggregation.
"""
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from . import app_contract as contract
from .analytics_reader import instant
from .classifier import normalize
from .contracts import SOURCES as POLICIES
from .food_observations import mention_policy
from .ingestion import METRIC_STORAGE

KST = timezone(timedelta(hours=9), 'KST')  # Korea has no daylight saving time
B8_SOURCES = ('google_trends_rss', 'instagram_graph')
SCOPES = {'google_trends_rss': 'SHARED_FEED', 'instagram_graph': 'PER_FOOD_QUERY'}
OBSERVED_COVERAGE = ('COMPLETE', 'PARTIAL', 'LIMITED')
RANK = {'NONE': 0, 'PARTIAL': 1, 'LIMITED': 2, 'COMPLETE': 3}
COVERAGE_OF = {v: k for k, v in RANK.items()}
LEGACY = 'legacy_unknown'
REASONS = {'youtube': 'YouTube 영상의 음식 언급 추출·파생 지표는 현재 정책상 비활성입니다.',
           'naver_datalab': 'NAVER 상대 검색 지수는 요청 범위 안에서만 의미가 있고 저장하지 않으므로 B8 집계에 포함하지 않습니다.'}
RANKING_NOTES = ('같은 출처·단위·기간의 고유 콘텐츠 수만 비교합니다. 출처를 합친 종합 점수는 제공하지 않습니다.',)
INSTAGRAM_NOTES = ('Instagram은 음식별 해시태그 검색 회차가 성공한 날만 관측합니다. 추적 해시태그 수와 페이지 상한에 따라 값의 범위가 다를 수 있습니다.',)
PROVIDER_VALUES = 5


class FoodNotFound(LookupError):
    pass


class SourceNotRankable(ValueError):
    def __init__(self, source, reason):
        super().__init__(source)
        self.source, self.reason = source, reason


def availability(source):
    """INCLUDED only where B8 counts the source and its policy still grants mention extraction."""
    if source in B8_SOURCES and mention_policy(source) == 'ALLOWED':
        return 'INCLUDED', None
    return ('NOT_STORED' if METRIC_STORAGE[source] == 'TRANSIENT_ONLY' else 'POLICY_BLOCKED'), REASONS.get(source, '정책상 B8 집계에서 제외된 출처입니다.')


def stamp(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if moment else None


def kst_day(moment):
    return moment.astimezone(KST).date()


def days_ending(end, count):
    return [end - timedelta(days=count - 1 - n) for n in range(count)]


def period(days):
    return {'start_date': days[0].isoformat(), 'end_date': days[-1].isoformat(), 'days': len(days), 'timezone': contract.TIMEZONE}


def _squash(text):
    return re.sub(r'\s+', '', normalize(text).casefold())


def match_kind(name, query):
    """3 exact, 2 prefix, 1 contains, 0 none; whitespace and case insensitive."""
    name, query = _squash(name), _squash(query)
    if name == query:
        return 3
    if name.startswith(query):
        return 2
    return 1 if query in name else 0


class Catalog:
    """Foods, merge redirects (transitive) and approved aliases of one read snapshot."""

    def __init__(self, session):
        self.foods = {f['food_id']: f for f in session.foods()}
        self.root = {food_id: self._root(food_id) for food_id in self.foods}
        self.members = defaultdict(list)
        for food_id in sorted(self.foods, key=lambda f: (f != self.root[f], f)):
            self.members[self.root[food_id]].append(food_id)
        self.aliases = defaultdict(list)
        for alias in sorted(session.aliases(), key=lambda a: (a['scope'], a['normalized_form'], a['alias_id'])):
            if alias['food_id'] in self.root:
                self.aliases[self.root[alias['food_id']]].append(alias)
        self.relations = session.relations()

    def _root(self, food_id):
        seen = set()
        while self.foods[food_id]['status'] == 'MERGED' and self.foods[food_id]['merged_into'] in self.foods and food_id not in seen:
            seen.add(food_id)
            food_id = self.foods[food_id]['merged_into']
        return food_id

    def resolve(self, food_id):
        if food_id not in self.foods:
            raise FoodNotFound(food_id)
        return self.root[food_id]

    def active(self, kind=None):
        return [f for f, row in self.foods.items() if row['status'] == 'ACTIVE' and (kind is None or row['entity_kind'] == kind)]

    def ref(self, food_id):
        row = self.foods[food_id]
        return {'food_id': food_id, 'canonical_name': row['canonical_name'], 'entity_kind': row['entity_kind']}

    def alias_texts(self, root):
        return list(dict.fromkeys(a['surface_form'] for a in self.aliases[root]))


class Cell:
    __slots__ = ('state', 'items', 'rank', 'ok', 'failed', 'legacy', 'warn')

    def __init__(self, state, items, rank, ok, failed, legacy, warn):
        self.state, self.items, self.rank, self.ok, self.failed, self.legacy, self.warn = state, items, rank, ok, failed, legacy, warn


class Observations:
    """Per-root daily item sets and observation scope of one source."""

    def __init__(self, source, shared):
        self.source, self.shared = source, shared
        self.items = defaultdict(lambda: defaultdict(set))
        self.warn = defaultdict(lambda: defaultdict(set))
        self.legacy = defaultdict(lambda: defaultdict(set))
        self.expired_days = defaultdict(set)
        self.last = {}
        self.scope = defaultdict(lambda: defaultdict(dict))  # owner -> day -> query -> best coverage rank
        self.runs = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # owner -> day -> [successful, failed]

    def add_item(self, root, day, item, warn, moment):
        self.items[root][day].add(item)
        if warn:
            self.warn[root][day].add(item)
        self.last[root] = max(self.last.get(root, moment), moment)

    def add_run(self, owner, day, query, rank):
        """One scope run; owner is None for the shared feed. The best run per query and day wins."""
        queries = self.scope[owner][day]
        queries[query] = max(queries.get(query, 0), rank)
        self.runs[owner][day][0 if rank else 1] += 1

    def cell(self, root, day):
        owner = None if self.shared else root
        queries = self.scope[owner].get(day, {}) if owner in self.scope else {}
        ok, failed = self.runs[owner][day] if owner in self.runs and day in self.runs[owner] else (0, 0)
        legacy = self.legacy[root].get(day, set()) if root in self.legacy else set()
        if not queries or max(queries.values()) == 0:
            return Cell('NOT_OBSERVED', set(), 0, ok, failed, legacy, set())
        # A query that failed all day next to successful ones leaves the day PARTIAL.
        rank = min(queries.values()) or RANK['PARTIAL']
        if day in self.expired_days.get(root, ()):
            return Cell('EVIDENCE_EXPIRED', set(), rank, ok, failed, legacy, set())
        items = self.items[root].get(day, set()) if root in self.items else set()
        warn = self.warn[root].get(day, set()) if root in self.warn else set()
        return Cell('OBSERVED', items, rank, ok, failed, legacy, warn)


def summarize(cells, days, today, strict):
    """Bucket or period over day cells. strict (period values) also refuses a day still being collected."""
    states = Counter(c.state for c in cells)
    incomplete = any(d >= today for d in days)
    if states['NOT_OBSERVED'] == len(cells):
        state = 'NOT_OBSERVED'
    elif states['OBSERVED'] == len(cells) and not (strict and incomplete):
        state = 'OBSERVED'
    elif len(cells) == 1 and not strict:
        state = cells[0].state  # a single day keeps its own state (EVIDENCE_EXPIRED)
    else:
        state = 'INSUFFICIENT_COVERAGE'
    union = set().union(*(c.items for c in cells))
    value = {'value': len(union) if state == 'OBSERVED' else None, 'value_state': state,
             'coverage': COVERAGE_OF[min((c.rank for c in cells), default=0)],
             'day_counts': {'observed': states['OBSERVED'], 'not_observed': states['NOT_OBSERVED'],
                            'evidence_expired': states['EVIDENCE_EXPIRED']},
             'includes_incomplete_day': incomplete}
    extras = {'items': union, 'legacy': set().union(*(c.legacy for c in cells)), 'warn': set().union(*(c.warn for c in cells)),
              'ok': sum(c.ok for c in cells), 'failed': sum(c.failed for c in cells)}
    return value, extras


def change(current, previous):
    def no(reason):
        return {'state': 'NOT_COMPARABLE', 'reason': reason, 'absolute': None, 'ratio': None}
    if current['value_state'] != 'OBSERVED':
        return no('CURRENT_PERIOD_INSUFFICIENT')
    if previous['value_state'] != 'OBSERVED':
        return no('PREVIOUS_PERIOD_INSUFFICIENT')
    if current['coverage'] != 'COMPLETE' or previous['coverage'] != 'COMPLETE':
        return no('COVERAGE_LIMITED')  # lower bounds do not make a growth rate
    absolute = current['value'] - previous['value']
    if previous['value'] == 0:
        return {'state': 'COMPARABLE', 'reason': 'PREVIOUS_ZERO', 'absolute': absolute, 'ratio': None}
    return {'state': 'COMPARABLE', 'reason': None, 'absolute': absolute, 'ratio': round(absolute / previous['value'], 4)}


class FoodAnalytics:
    """Read-time B8 analytics over an analytics store (SQLite application DB or B6 target)."""

    def __init__(self, store, clock=None, origin='live_api'):
        if origin not in ('live_api', 'mock'):
            raise ValueError('observation origin must be live_api or mock')
        self.store, self.origin = store, origin
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        return instant(self.clock())

    def today(self):
        return kst_day(self.now())

    def default_end(self):
        return self.today() - timedelta(days=1)

    # Building blocks

    def _observations(self, session, source, catalog, ids=None):
        obs = Observations(source, SCOPES[source] == 'SHARED_FEED')
        self._add_items(obs, session.confirmed(source, ids), catalog)
        counted = set()
        for owner, run_id, query, moment, origin, rank in self._scope_rows(session, obs, catalog, ids):
            if origin == self.origin and (obs.shared or owner is not None) and (owner, run_id) not in counted:
                counted.add((owner, run_id))  # a run serving two merged foods counts once for their root
                obs.add_run(owner, kst_day(moment), query, rank)
        return obs

    def _add_items(self, obs, rows, catalog):
        now, valid, stale = self.now(), defaultdict(set), defaultdict(set)
        for row in rows:
            fresh = row['mention_status'] == 'ACTIVE' and row['availability'] == 'AVAILABLE' and (
                row['mention_expires_at'] is None or row['mention_expires_at'] > now)
            (valid if fresh else stale)[catalog.root.get(row['food_id'])].add(row['item_key'])
        for row in rows:
            root, item, day = catalog.root.get(row['food_id']), row['item_key'], kst_day(row['observed_at'])
            if row['origin'] == LEGACY:
                if item in valid[root]:
                    obs.legacy[root][day].add(item)
            elif row['origin'] != self.origin or row['coverage'] not in OBSERVED_COVERAGE:
                continue
            elif item in valid[root]:
                obs.add_item(root, day, item, row['quality_status'] == 'WARN', row['observed_at'])
            elif item in stale[root]:
                obs.expired_days[root].add(day)  # evidence gone: the day can no longer be recounted

    @staticmethod
    def _scope_rows(session, obs, catalog, ids):
        """(owner, run, query, observed_at, origin, coverage rank) of the runs that define observed days."""
        if obs.shared:
            for r in session.runs(obs.source):
                yield None, r['ingest_run_id'], 'feed', r['observed_at'], r['origin'], RANK.get(r['coverage'], 0)
            return
        for r in session.food_scope(obs.source, ids):
            observed = r['value_state'] == 'OBSERVED' and r['coverage'] in OBSERVED_COVERAGE
            yield (catalog.root.get(r['food_id']), r['ingest_run_id'], r['query_id'] or r['ingest_run_id'], r['observed_at'], r['origin'],
                   RANK[r['coverage']] if observed else 0)

    def _source_base(self, source):
        state, reason = availability(source)
        return {'source_code': source, 'source_name': contract.SOURCE_NAMES[source], 'availability': state, 'availability_reason': reason}

    def _sources(self, source):
        return (source,) if source else contract.SOURCE_CODES

    def _pending_roots(self, session, catalog):
        roots = set()
        for event in session.pending_entity_events():
            before = json.loads(event['before_json']) if event['before_json'] else {}
            after = json.loads(event['after_json']) if event['after_json'] else {}
            ids = {before.get('food_id'), after.get('food_id'), after.get('merged_into')}
            if event['subject_type'] == 'food':
                ids.add(event['subject_id'])
            roots.update(catalog.root[i] for i in ids if i in catalog.root)
        return roots

    def _metric(self, source):
        return {'metric_code': contract.METRIC_CODE, 'unit': contract.METRIC_UNIT, 'item_label': contract.ITEM_LABELS[source]}

    # Read models

    def health(self):
        return {'schema_ready': self.store.ready()}

    def foods(self, q=None, kind=None, sort='name'):
        with self.store.session() as session:
            catalog = Catalog(session)
            last = {}
            for source in B8_SOURCES:
                for root, moment in self._observations(session, source, catalog).last.items():
                    last[root] = max(last.get(root, moment), moment)
        items, folded = [], normalize(q).casefold() if q else None
        for root in catalog.active(kind):
            row, match, relevance = catalog.foods[root], None, None
            if q:
                names = [('canonical_name', row['canonical_name'])] + [('alias', a) for a in catalog.alias_texts(root)]
                # Kind first; then a spelling that matches as typed (spaces kept), the canonical name, alias order.
                scored = [(match_kind(name, q), folded in normalize(name).casefold(), field == 'canonical_name', -n, field, name)
                          for n, (field, name) in enumerate(names)]
                best = max(scored)
                if best[0] == 0:
                    continue
                match = {'field': best[4], 'text': best[5], 'kind': {3: 'exact', 2: 'prefix', 1: 'contains'}[best[0]]}
                relevance = best[:3]
            items.append((dict(catalog.ref(root), aliases=catalog.alias_texts(root), last_observed_at=stamp(last.get(root)), match=match),
                          relevance, last.get(root)))
        if sort == 'relevance':
            items.sort(key=lambda e: (tuple(-int(v) for v in e[1]), e[0]['canonical_name'], e[0]['food_id']))
        elif sort == '-last_observed_at':
            items.sort(key=lambda e: (e[2] is None, -(e[2].timestamp() if e[2] else 0), e[0]['canonical_name'], e[0]['food_id']))
        else:
            items.sort(key=lambda e: (e[0]['canonical_name'], e[0]['food_id']))
        return [e[0] for e in items]

    def food(self, food_id):
        with self.store.session() as session:
            catalog = Catalog(session)
            root = catalog.resolve(food_id)
            ids = catalog.members[root]
            last = {s: self._observations(session, s, catalog, ids).last.get(root) for s in B8_SOURCES}
            pending = root in self._pending_roots(session, catalog)
        row, relations = catalog.foods[root], []
        for rel in sorted(catalog.relations, key=lambda r: (r['relation_type'], r['from_food_id'], r['to_food_id'])):
            ends = (catalog.root.get(rel['from_food_id']), catalog.root.get(rel['to_food_id']))
            if root not in ends or None in ends or ends[0] == ends[1]:
                continue
            other = ends[1] if ends[0] == root else ends[0]
            kind = rel['relation_type'] if rel['relation_type'] == 'RELATED_TO' or ends[0] == root else 'HAS_VARIANT'
            relations.append({'relation_type': kind, 'food': catalog.ref(other)})
        sources = [dict(self._source_base(s), last_observed_at=stamp(last.get(s))) for s in contract.SOURCE_CODES]
        observed = [m for m in last.values() if m]
        return dict(catalog.ref(root), status=row['status'],
                    aliases=[{'text': a['surface_form'], 'scope': a['scope']} for a in catalog.aliases[root]],
                    relations=relations, merged_food_ids=[i for i in ids if i != root],
                    redirect={'requested_food_id': food_id, 'reason': 'MERGED'} if food_id != root else None,
                    created_at=stamp(row['created_at']), updated_at=stamp(row['updated_at']), sources=sources,
                    freshness={'last_observed_at': stamp(max(observed)) if observed else None, 'reprocessing_pending': pending})

    def timeline(self, food_id, days, end, granularity='day', source=None):
        today, window = self.today(), days_ending(end, days)
        with self.store.session() as session:
            catalog = Catalog(session)
            root = catalog.resolve(food_id)
            series = []
            for src in self._sources(source):
                entry = dict(self._source_base(src), metric=None, points=[])
                if entry['availability'] == 'INCLUDED':
                    obs = self._observations(session, src, catalog, catalog.members[root])
                    cells = {d: obs.cell(root, d) for d in window}
                    entry.update(metric=self._metric(src), points=self._points(cells, window, today, granularity))
                series.append(entry)
        return {'food': catalog.ref(root), 'period': period(window), 'granularity': granularity, 'series': series}

    @staticmethod
    def _points(cells, window, today, granularity):
        buckets = defaultdict(list)
        for d in window:
            buckets[d - timedelta(days=d.weekday()) if granularity == 'week' else d].append(d)
        points = []
        for key in sorted(buckets):
            days = buckets[key]
            value, extras = summarize([cells[d] for d in days], days, today, strict=False)
            points.append({'start_date': days[0].isoformat(), 'end_date': days[-1].isoformat(), 'days': len(days),
                           'value': value['value'], 'value_state': value['value_state'], 'coverage': value['coverage'],
                           'observed_days': value['day_counts']['observed'],
                           'complete': not value['includes_incomplete_day'] and (granularity == 'day' or len(days) == 7),
                           'runs': {'successful': extras['ok'], 'failed': extras['failed']},
                           'legacy_unknown_value': len(extras['legacy']) or None})
        return points

    def metrics(self, food_id, days, end, source=None):
        today, current_days = self.today(), days_ending(end, days)
        previous_days = days_ending(current_days[0] - timedelta(days=1), days)
        with self.store.session() as session:
            catalog = Catalog(session)
            root = catalog.resolve(food_id)
            ids, sources = catalog.members[root], []
            for src in self._sources(source):
                entry = dict(self._source_base(src), metrics=[], provider_values=[], quality=None, last_observed_at=None, legacy_unknown=None)
                if entry['availability'] == 'INCLUDED':
                    obs = self._observations(session, src, catalog, ids)
                    current, extras = summarize([obs.cell(root, d) for d in current_days], current_days, today, strict=True)
                    previous, _ = summarize([obs.cell(root, d) for d in previous_days], previous_days, today, strict=True)
                    entry.update(
                        metrics=[dict(self._metric(src), origin=self.origin, current=current, previous=previous,
                                      change=change(current, previous), rankable=True)],
                        provider_values=self._provider_values(session, ids, current_days) if src == 'google_trends_rss' else [],
                        quality={'checked_items': len(extras['items']), 'warn_items': len(extras['warn']), 'failed_runs': extras['failed']},
                        last_observed_at=stamp(obs.last.get(root)),
                        legacy_unknown={'value': len(extras['legacy']), 'coverage': 'UNKNOWN', 'origin': LEGACY} if extras['legacy'] else None)
                sources.append(entry)
        return {'food': catalog.ref(root), 'period': period(current_days), 'previous_period': period(previous_days), 'sources': sources}

    def _provider_values(self, session, ids, window):
        latest = {}
        for row in session.keyword_traffic(ids):
            if row['origin'] == self.origin and window[0] <= kst_day(row['observed_at']) <= window[-1]:
                if row['item_key'] not in latest or row['observed_at'] > latest[row['item_key']]['observed_at']:
                    latest[row['item_key']] = row
        rows = sorted(latest.values(), key=lambda r: (r['observed_at'], r['item_key']), reverse=True)[:PROVIDER_VALUES]
        return [{'metric_code': 'rss.approx_traffic', 'unit': 'approximate_search_traffic_text', 'raw_value': r['raw_value'],
                 'keyword': r['keyword'], 'observed_at': stamp(r['observed_at']), 'aggregatable': False} for r in rows]

    def rankings(self, source, days, end, kind=None, include_zero=False):
        state, _ = availability(source)
        if state != 'INCLUDED':
            raise SourceNotRankable(source, 'not_stored' if state == 'NOT_STORED' else 'policy_blocked')
        today, current_days = self.today(), days_ending(end, days)
        previous_days = days_ending(current_days[0] - timedelta(days=1), days)
        with self.store.session() as session:
            catalog = Catalog(session)
            obs = self._observations(session, source, catalog)
        entries, excluded = [], Counter()
        for root in catalog.active(kind):
            current, _ = summarize([obs.cell(root, d) for d in current_days], current_days, today, strict=True)
            if current['value_state'] != 'OBSERVED':
                excluded['not_observed' if current['value_state'] == 'NOT_OBSERVED' else 'insufficient_coverage'] += 1
                continue
            if current['value'] == 0 and not include_zero:
                excluded['zero_value'] += 1
                continue
            previous, _ = summarize([obs.cell(root, d) for d in previous_days], previous_days, today, strict=True)
            entries.append({'food': catalog.ref(root), 'current': current, 'previous': previous, 'change': change(current, previous)})
        entries.sort(key=lambda e: (-e['current']['value'], e['food']['canonical_name'], e['food']['food_id']))
        for position, entry in enumerate(entries):
            tied = position and entries[position - 1]['current']['value'] == entry['current']['value']
            entry['rank'] = entries[position - 1]['rank'] if tied else position + 1
        basis = {'source_code': source, 'source_name': contract.SOURCE_NAMES[source], 'metric': self._metric(source),
                 'period': period(current_days), 'previous_period': period(previous_days), 'scope': 'SINGLE_SOURCE',
                 'observation_scope': SCOPES[source], 'notes': list(RANKING_NOTES + (INSTAGRAM_NOTES if source == 'instagram_graph' else ()))}
        return {'basis': basis, 'items': [{'rank': e['rank'], **e} for e in entries],
                'excluded': {k: excluded[k] for k in ('insufficient_coverage', 'not_observed', 'zero_value')}}

    def sources_status(self, days):
        today = self.today()
        window = days_ending(today, days)
        with self.store.session() as session:
            pending = len(session.pending_entity_events())
            sources = [self._status(session, s, window) for s in contract.SOURCE_CODES]
        successes = [moment for moment in (s.pop('_last_success') for s in sources) if moment]
        return {'data_through': stamp(max(successes)) if successes else None, 'window': period(window), 'sources': sources,
                'aggregation': {'mode': 'read_time', 'materialized': False, 'pending_entity_changes': pending}}

    def _status(self, session, source, window):
        policy = POLICIES[source]['policy']
        entry = dict(self._source_base(source),
                     policy={'policy_version': policy['version'], 'retention_days': policy['retention_days'],
                             'mention_extraction': mention_policy(source), 'metric_storage': METRIC_STORAGE[source]},
                     observation_scope=SCOPES.get(source), last_run_at=None, last_successful_run_at=None, latest_coverage=None,
                     window_stats=None, quality=None, legacy_unknown=None, _last_success=None)
        if entry['availability'] != 'INCLUDED':
            return entry
        runs = sorted((r for r in session.runs(source) if r['origin'] == self.origin), key=lambda r: (r['observed_at'], r['ingest_run_id']))
        good = [r for r in runs if r['coverage'] in OBSERVED_COVERAGE]
        inside = [r for r in runs if window[0] <= kst_day(r['observed_at']) <= window[-1]]
        outcomes = Counter()
        ids = {r['ingest_run_id'] for r in inside}
        for row in session.quality(source):
            if row['ingest_run_id'] in ids:
                outcomes[row['outcome']] += int(row['n'])
        coverage_counts = Counter(r['coverage'] for r in inside)
        legacy = session.legacy_summary(source)
        entry.update(last_run_at=stamp(runs[-1]['observed_at']) if runs else None,
                     last_successful_run_at=stamp(good[-1]['observed_at']) if good else None,
                     latest_coverage=runs[-1]['coverage'] if runs and runs[-1]['coverage'] in RANK else None,
                     window_stats={'runs_total': len(inside), 'runs_successful': sum(r['coverage'] in OBSERVED_COVERAGE for r in inside),
                                   'runs_failed': sum(r['coverage'] == 'NONE' for r in inside),
                                   'observed_days': len({kst_day(r['observed_at']) for r in inside if r['coverage'] in OBSERVED_COVERAGE}),
                                   'coverage_counts': {c: coverage_counts[c] for c in contract.COVERAGES}},
                     quality={'fail_results': outcomes['FAIL'], 'warn_results': outcomes['WARN'],
                              'rejected_items': sum(int(r['rejected_items'] or 0) for r in inside)},
                     legacy_unknown={'runs': legacy['runs'], 'items': legacy['items'], 'coverage': 'UNKNOWN'} if legacy['runs'] else None,
                     _last_success=good[-1]['observed_at'] if good else None)
        return entry


def parse_day(text):
    """YYYY-MM-DD only (date.fromisoformat also accepts other ISO forms)."""
    if not isinstance(text, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        raise ValueError(text)
    return date.fromisoformat(text)
