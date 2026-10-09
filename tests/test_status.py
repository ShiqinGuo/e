from textual.app import App, ComposeResult

from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import TokenMeasurement
from agent_client.presentation.status import ContextMeter, MotionMode, WorkIndicator


class StatusApp(App):
    def compose(self) -> ComposeResult:
        yield WorkIndicator()
        yield ContextMeter(ContextUsage(context_window=256000, input_budget=235008))


async def test_context_meter_distinguishes_unknown_zero_and_overflow():
    app = StatusApp()
    async with app.run_test():
        meter = app.query_one(ContextMeter)
        assert str(meter.render()) == "Context -- / 256k · unavailable"
        meter.show_usage(
            ContextUsage(
                used_tokens=64000,
                context_window=256000,
                input_budget=235008,
                measurement=TokenMeasurement.PROVIDER_INPUT_TOKENS,
            )
        )
        assert str(meter.render()) == "Context 64.0k / 256k · 25.0% · last input"
        meter.show_usage(
            ContextUsage(
                used_tokens=0,
                context_window=256000,
                input_budget=235008,
                measurement=TokenMeasurement.PROVIDER_INPUT_TOKENS,
            )
        )
        assert str(meter.render()) == "Context 0.0k / 256k · 0.0% · last input"
        meter.show_usage(
            ContextUsage(
                used_tokens=281600,
                context_window=256000,
                input_budget=235008,
                measurement=TokenMeasurement.PROVIDER_INPUT_TOKENS,
            )
        )
        assert str(meter.render()) == "Context 281.6k / 256k · 110.0% · last input"


async def test_reduced_motion_keeps_work_indicator_symbol_still():
    app = StatusApp()
    async with app.run_test():
        app.animation_level = MotionMode.NONE.value
        indicator = app.query_one(WorkIndicator)
        indicator.set_activity("Waiting for model", "", working=True)
        symbol = str(indicator.render()).split()[0]
        for _ in range(5):
            indicator.advance()
            assert str(indicator.render()).split()[0] == symbol
        indicator.set_activity("Idle", "", working=False)
        assert str(indicator.render()) == "Idle"
