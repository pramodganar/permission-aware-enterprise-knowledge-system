"""The arithmetic of the latency measurement: percentiles, the filter's cost, the summary."""

import time

import pytest

from evaluation.latency import Clock, Timed, milliseconds, percentile, summary


def timed(
    question_id: str = "q",
    total: float = 0.1,
    unfiltered: float = 0.001,
    failed: str | None = None,
    **stages: float,
) -> Timed:
    return Timed(
        question_id=question_id,
        stages=stages,
        total=total,
        unfiltered_search=unfiltered,
        answered=True,
        reached_model="model" in stages,
        failed=failed,
    )


def test_the_nearest_rank_percentile() -> None:
    values = [float(v) for v in range(1, 11)]
    assert percentile(values, 50) == 5.0
    assert percentile(values, 90) == 9.0
    # 95 percent of ten values is 9.5, so it takes the tenth.
    assert percentile(values, 95) == 10.0
    assert percentile(values, 100) == 10.0
    assert percentile(values, 0) == 1.0
    assert percentile([3.0], 95) == 3.0
    # The order they come in doesn't matter.
    assert percentile(list(reversed(values)), 50) == 5.0


def test_a_percentile_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


def test_times_are_reported_in_milliseconds() -> None:
    assert milliseconds([0.001, 0.002, 0.003, 0.010]) == {
        "count": 4,
        "p50": 2.0,
        "p95": 10.0,
        "max": 10.0,
        "mean": 4.0,
    }


def test_the_filter_costs_the_scope_the_checks_and_the_narrower_search() -> None:
    t = timed(scope=0.002, can_read=0.0005, search=0.003, unfiltered=0.001)
    assert t.filter_cost == pytest.approx(0.002 + 0.0005 + (0.003 - 0.001))


def test_an_empty_scope_saves_the_search_so_the_filter_can_cost_less_than_nothing() -> None:
    # An asker who reads nothing isn't searched for, while an unfiltered
    # system would still search everything.
    assert timed(unfiltered=0.001).filter_cost == pytest.approx(-0.001)


def test_the_summary_counts_each_stage_where_it_ran() -> None:
    numbers = summary(
        [
            timed("a", total=0.020, embed=0.010, scope=0.002, search=0.003, load_and_check=0.002),
            timed(
                "b",
                total=0.050,
                embed=0.012,
                scope=0.002,
                search=0.003,
                load_and_check=0.002,
                rerank=0.020,
                model=0.008,
            ),
        ]
    )
    stages = numbers["stages_ms"]
    assert stages["embed"]["count"] == 2
    assert stages["rerank"] == {"count": 1, "p50": 20.0, "p95": 20.0, "max": 20.0, "mean": 20.0}
    assert "context" not in stages
    # The retriever's own stages, reranking included where it ran.
    assert stages["retrieval"]["max"] == 27.0
    assert stages["retrieval"]["p50"] == 7.0
    assert numbers["reached_model"] == 1
    assert numbers["end_to_end_answered_ms"]["p50"] == 50.0


def test_a_failed_question_is_listed_and_timed_for_nothing() -> None:
    numbers = summary(
        [
            timed("a", total=0.020, embed=0.010),
            timed("b", total=9.0, embed=0.010, model=8.9, failed="LLMUnavailableError"),
        ]
    )
    assert numbers["failed"] == ["b"]
    assert numbers["stages_ms"]["total"]["max"] == 20.0
    assert "model" not in numbers["stages_ms"]
    assert numbers["end_to_end_answered_ms"] is None


def test_the_filters_share_is_of_the_whole_question() -> None:
    numbers = summary([timed(total=0.1, unfiltered=0.0, scope=0.01, search=0.0, can_read=0.0)] * 3)
    assert numbers["filter_share_of_total_p50"] == 0.1


def test_a_stage_called_several_times_in_one_question_adds_up() -> None:
    # can_read runs once for every chunk found.
    clock = Clock()
    slow = clock.timed("can_read", lambda: time.sleep(0.01))
    for _ in range(3):
        slow()
    assert clock.spent["can_read"] >= 0.03


def test_a_stage_that_fails_is_still_timed_and_still_fails() -> None:
    def fails() -> None:
        time.sleep(0.01)
        raise RuntimeError("the model gave up")

    clock = Clock()
    with pytest.raises(RuntimeError, match="gave up"):
        clock.timed("model", fails)()
    assert clock.spent["model"] >= 0.01
    assert "model" not in clock.returned


def test_a_stage_hands_back_what_its_function_returned() -> None:
    clock = Clock()
    assert clock.timed("embed", lambda x: x * 2)(21) == 42
    assert clock.returned["embed"] == 42
