import random
from collections import Counter
from decimal import Decimal

import pytest

from steelplan.models import OptimizeRequest
from steelplan.optimizer import MIN_REMAINDER, optimize, solve_group, units


def request(lengths, stocks=(6000,), kerf=0):
    return OptimizeRequest(items=[{'material': 'SS275', 'spec': 'H-300', 'length': length, 'quantity': count}
                                  for length, count in Counter(lengths).items()],
                           stock_lengths=list(stocks), kerf=kerf, time_limit=3, objective="bar_count")


def verify(plan, req):
    expected = Counter()
    for i in req.items:
        expected[i.material, i.spec, i.length] += i.quantity
    actual = Counter()
    stock_total = part_total = kerf_total = rem_total = Decimal(0)
    for p in plan['patterns']:
        assert p['stock_length'] in req.stock_lengths
        assert Decimal(str(p['remnant'])) > 30
        parts = Decimal(0)
        for cut in p['cuts']:
            length = Decimal(str(cut['length']))
            actual[p['material'], p['spec'], length] += cut['quantity'] * p['bar_count']
            parts += length * cut['quantity']
        kerf = req.kerf * sum(c['quantity'] for c in p['cuts'])
        assert parts + kerf + Decimal(str(p['remnant'])) == Decimal(str(p['stock_length']))
        stock_total += Decimal(str(p['stock_length'])) * p['bar_count']
        part_total += parts * p['bar_count']
        kerf_total += kerf * p['bar_count']
        rem_total += Decimal(str(p['remnant'])) * p['bar_count']
    assert actual == expected
    s = plan['summary']
    assert Decimal(str(s['total_stock_length'])) == stock_total
    assert Decimal(str(s['total_parts_length'])) == part_total
    assert Decimal(str(s['total_kerf_loss'])) == kerf_total
    assert Decimal(str(s['total_remnant'])) == rem_total
    assert s['loss_rate'] == round(float(100 * (stock_total - part_total) / stock_total), 2)
    assert sum(o['quantity'] for o in plan['orders']) == s['bar_count']


@pytest.mark.parametrize('length', [6000, 5999, 5970])
def test_exact_fit_and_remainder_at_most_30_rejected(length):
    with pytest.raises(ValueError, match='들어가지'):
        optimize(request([length]))


def test_strict_decimal_boundary_and_kerf():
    req = request(['5966.999'], kerf=3)
    plan = optimize(req)
    assert plan['patterns'][0]['remnant'] == 30.001
    verify(plan, req)
    with pytest.raises(ValueError):
        optimize(request([5967], kerf=3))


def test_two_pieces_that_leave_30_must_use_two_bars():
    req = request([2985, 2985])
    plan = optimize(req)
    assert plan['summary']['bar_count'] == 2
    verify(plan, req)


def test_quantity_first_then_shorter_stock():
    req = request([2950] * 4, stocks=(6000, 9000, 12000))
    plan = optimize(req)
    assert plan['summary']['bar_count'] == 1
    assert plan['orders'][0]['stock_length'] == 12000
    verify(plan, req)
    req = request([2000] * 2, stocks=(6000, 9000, 12000))
    plan = optimize(req)
    assert plan['orders'][0]['stock_length'] == 6000


def test_material_spec_isolation_and_duplicate_rows():
    req = OptimizeRequest(items=[{'material': m, 'spec': s, 'length': 2000, 'quantity': 1}
                                  for m, s in [('SS275', 'A'), ('SM355', 'A'), ('SS275', 'B'), ('SS275', 'A')]],
                           stock_lengths=[6000], kerf=3)
    plan = optimize(req)
    assert plan['summary']['group_count'] == 3
    assert plan['summary']['bar_count'] == 3
    verify(plan, req)


def test_fallback_never_claims_optimal_and_is_feasible():
    bars, status, detail, _ = solve_group([units(2000)], [5], [units(6000)], units(3), 0)
    assert status == 'FEASIBLE'
    assert sum(b['counts'][0] for b in bars) == 5
    assert all(b['stock'] - b['counts'][0] * units(2003) >= MIN_REMAINDER for b in bars)


def brute_force(lengths, stocks, kerf):
    """Independent exhaustive set-partition oracle for small test inputs."""
    sizes = sorted([units(l) + units(kerf) for l in lengths], reverse=True)
    capacities = [units(s) for s in stocks]
    best = (len(lengths) + 1, 10**18)

    def visit(index, bins):
        nonlocal best
        if len(bins) > best[0]:
            return
        if index == len(sizes):
            candidate = (len(bins), sum(next(s for s in capacities if s - load >= MIN_REMAINDER) for load in bins))
            best = min(best, candidate)
            return
        seen = set()
        for j, load in enumerate(bins):
            if load in seen or load + sizes[index] > capacities[-1] - MIN_REMAINDER:
                continue
            seen.add(load)
            bins[j] += sizes[index]
            visit(index + 1, bins)
            bins[j] -= sizes[index]
        visit(index + 1, bins + [sizes[index]])

    visit(0, [])
    return best


def test_solver_matches_exhaustive_oracle():
    rng = random.Random(438)
    for _ in range(25):
        lengths = [rng.randrange(500, 5000, 250) for _ in range(rng.randint(2, 7))]
        stocks = (6000, 9000, 12000)
        kerf = rng.choice([0, 3, 4.5])
        req = request(lengths, stocks, kerf)
        plan = optimize(req)
        assert plan['summary']['optimal']
        assert (plan['summary']['bar_count'], units(plan['summary']['total_stock_length'])) == brute_force(lengths, stocks, kerf)
        verify(plan, req)


def test_input_validation_and_total_limit():
    for invalid in [True, 0, -1, 1.5]:
        with pytest.raises(ValueError):
            OptimizeRequest(items=[{'material': 'A', 'spec': 'B', 'length': 1000, 'quantity': invalid}], stock_lengths=[6000])
    with pytest.raises(ValueError):
        request(['1234.5678'])
    req = OptimizeRequest(items=[{'material': 'A', 'spec': 'B', 'length': 1000, 'quantity': 3000}] * 2,
                           stock_lengths=[6000])
    with pytest.raises(ValueError, match='5,000'):
        optimize(req)
