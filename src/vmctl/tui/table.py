"""Table mouse events retain the exact row and cell that were clicked."""

from textual import events
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable


class VMTable(DataTable[str]):
    class Clicked(Message):
        def __init__(self, vmid: int, column: int, value: str) -> None:
            super().__init__()
            self.vmid, self.column, self.value = vmid, column, value

    async def _on_click(self, event: events.Click) -> None:
        # Textual supplies rendered cell coordinates in the mouse style metadata.
        meta = event.style.meta
        row, column = meta.get("row"), meta.get("column")
        if meta.get("out_of_bounds", False):
            event.stop()
            return
        if (
            event.button != 1
            or not isinstance(row, int)
            or not isinstance(column, int)
            or row < 0
            or column < 0
            or row >= self.row_count
            or column >= len(self.columns)
        ):
            await super()._on_click(event)
            return
        coordinate = Coordinate(row, column)
        key = self.coordinate_to_cell_key(coordinate).row_key.value
        if key is not None:
            self.cursor_coordinate = coordinate
            self.focus()
            self.post_message(self.Clicked(int(key), column, self.get_cell_at(coordinate)))
        event.prevent_default()
        event.stop()
