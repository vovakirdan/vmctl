"""Frontend selection state backed by the shared dependency resolver."""

from dataclasses import dataclass, field

from vmctl.bootstrap.resolver import resolve_definitions
from vmctl.errors import VmctlError
from vmctl.operations import Catalog, CatalogItem


@dataclass
class FeatureSelection:
    catalog: Catalog
    template: str
    system: set[str] = field(default_factory=set)
    development: set[str] = field(default_factory=set)

    def set_template(self, template: str) -> None:
        self.template = template
        self.system = set(self.catalog.system_defaults.get(template, ()))
        compatible = {
            item.name
            for item in (*self.catalog.modules, *self.catalog.profiles)
            if template in item.templates
        }
        self.development.intersection_update(compatible)

    def resolve(self, *, system: bool) -> tuple[CatalogItem, ...]:
        items = self.catalog.system_features if system else self.catalog.modules
        selected = self.system if system else self.development
        profiles = {} if system else {item.name: item.members for item in self.catalog.profiles}
        return resolve_definitions(
            sorted(selected),
            {item.name: item for item in items},
            profiles,
            category="System" if system else "Development",
        )

    def selected_names(self, *, system: bool) -> tuple[str, ...]:
        return tuple(item.name for item in self.resolve(system=system))

    def required_names(self, *, system: bool) -> set[str]:
        selected = self.system if system else self.development
        required: set[str] = set()
        for name in selected:
            items = self.catalog.system_features if system else self.catalog.modules
            profiles = {} if system else {item.name: item.members for item in self.catalog.profiles}
            resolved = resolve_definitions((name,), {item.name: item for item in items}, profiles)
            required.update(item.name for item in resolved if item.name != name)
        return required

    def toggle(self, name: str, value: bool, *, system: bool) -> None:
        selected = self.system if system else self.development
        previous = selected.copy()
        try:
            if value:
                selected.add(name)
            elif name not in self.required_names(system=system):
                selected.discard(name)
            for item in self.resolve(system=system):
                if self.template not in item.templates:
                    raise VmctlError(f"{item.name} is not supported by template {self.template}")
        except VmctlError:
            selected.clear()
            selected.update(previous)
            raise

    def without_system(self) -> tuple[str, ...]:
        active = set(self.selected_names(system=True))
        return tuple(
            name
            for name in self.catalog.system_defaults.get(self.template, ())
            if name not in active
        )
