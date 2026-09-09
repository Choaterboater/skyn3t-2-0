import { joinSession } from "@github/copilot-sdk/extension";
import { ActivityObserver, ObservationError } from "./monitor.mjs";
import { attachLifecycle, createActivityTools } from "./tools.mjs";

let observer;
const session = await joinSession({
    tools: createActivityTools(() => {
        if (!observer) throw new ObservationError("not_ready");
        return observer;
    }),
});
observer = new ActivityObserver({
    workspacePath: session.workspacePath,
    log: (message, options) => session.log(message, options),
});
attachLifecycle(session, observer);
