"""Type gate independent of legacy classifier labels and history."""
FOOD_KINDS = frozenset(('DISH', 'PRODUCT', 'INGREDIENT', 'FOOD_CONCEPT'))
CANDIDATE_KINDS = FOOD_KINDS | {'UNKNOWN', 'BRAND', 'STORE', 'NON_FOOD'}
BLOCKED_LEGACY_TYPES = frozenset(('FOOD_BRAND', 'BRAND', 'STORE', 'LOCATION',
                                 'ORGANIZATION', 'PERSON', 'NON_FOOD_PRODUCT', 'EVENT'))


def candidate_kind(entity_type, classification):
    if entity_type in ('FOOD_BRAND', 'BRAND', 'ORGANIZATION'):
        return 'BRAND'
    if entity_type == 'STORE':
        return 'STORE'
    if entity_type in BLOCKED_LEGACY_TYPES or classification == 'NON_FOOD':
        return 'NON_FOOD'
    return 'PRODUCT' if entity_type == 'FOOD_PRODUCT' else 'UNKNOWN'


def can_track_legacy(entity_type):
    return entity_type not in BLOCKED_LEGACY_TYPES
