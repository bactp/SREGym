"""A class representing a composite of mulitple applications"""

from concurrent.futures import ThreadPoolExecutor

from sregym.service.apps.base import Application


class CompositeApp:
    def __init__(self, apps: list[Application]):
        self.apps = {}
        for app in apps:
            if app.name in self.apps:
                print(f"[CompositeApp] same app name: {app.name}, continue.")
                continue
            self.apps[app.name] = app
        print(f"[CompositeApp] Apps: {self.apps}")
        self.name = "CompositeApp"
        self.app_name = "CompositeApp"
        self.namespaces = [app.namespace for app in self.apps.values()]
        # Kept for backwards compatibility; consumers that expect a single
        # namespace should prefer `namespaces` for composite apps.
        self.namespace = self.namespaces[0] if self.namespaces else None
        self.description = f"Composite application containing {len(self.apps)} apps: {', '.join(self.apps.keys())}"

    def deploy(self):
        def deploy_app(app):
            print(f"[CompositeApp] Deploying {app.name}...")
            app.deploy()

        with ThreadPoolExecutor() as executor:
            # executor.map() submits every future immediately, but only
            # raises a sub-app's exception once its result is consumed --
            # `list(...)` forces that, so a broken deploy fails loudly
            # instead of silently leaving that app undeployed.
            list(executor.map(deploy_app, self.apps.values()))

    def start_workload(self):
        def start_workload_app(app):
            print(f"[CompositeApp] Starting workload for {app.name}...")
            app.start_workload()

        with ThreadPoolExecutor() as executor:
            list(executor.map(start_workload_app, self.apps.values()))

    def cleanup(self):
        def cleanup_app(app):
            print(f"[CompositeApp] Cleaning up {app.name}...")
            try:
                app.cleanup()
            except Exception as e:
                # Best-effort: one app's cleanup failing must not hide the
                # failure (executor.map()'s result was never consumed before,
                # so this used to be silently swallowed) and must not stop
                # the other apps -- each already runs in its own thread, so
                # this only affects visibility, not whether they get attempted.
                print(f"[CompositeApp] Cleanup failed for {app.name}: {e}")

        with ThreadPoolExecutor() as executor:
            list(executor.map(cleanup_app, self.apps.values()))
