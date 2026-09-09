import { ObservationError } from "./monitor.mjs";

function result(value, resultType = "success") {
    return { textResultForLlm: JSON.stringify(value), resultType };
}

function validArgs(args, requireId) {
    return args !== null && typeof args === "object" && !Array.isArray(args)
        && Object.keys(args).every((key) => requireId && key === "monitor_id")
        && (!requireId || (typeof args.monitor_id === "string"
            && /^[a-f0-9-]{36}$/u.test(args.monitor_id)));
}

export function createActivityTools(getObserver) {
    const definitions = [
        {
            name: "skyn3t_activity_watch",
            description: "Call before a long SkyN3t studio improve/build run to show actual native timeline updates. Allocates a private future JSONL path and passively observes it; does not start a job. Start the NORMAL CLI through Bash with --activity-file afterwards and rely on Bash completion/outcome too.",
            id: false,
            run: (observer) => observer.watch(),
        },
        {
            name: "skyn3t_activity_status",
            description: "Get bounded, safe observations for a SkyN3t monitor, including latest stage/model/provider, event age and recent lines. Producer reports are not a substitute for the normal Bash process outcome. Accepts only an allocated monitor_id, never a file path.",
            id: true,
            run: (observer, args) => observer.get(args.monitor_id).status(),
        },
        {
            name: "skyn3t_activity_stop",
            description: "Stop only a SkyN3t activity feed and preserve its last observations for status. This does NOT cancel or stop SkyN3t; the normal Bash tool still owns the process and its completion notification.",
            id: true,
            run: async (observer, args) => ({
                ...await observer.get(args.monitor_id).stop(),
                message: "Activity feed stopped only. This does not cancel SkyN3t; the normal Bash tool still owns the process and its outcome.",
            }),
        },
    ];
    return definitions.map((definition) => ({
        name: definition.name,
        description: definition.description,
        parameters: {
            type: "object",
            properties: definition.id ? {
                monitor_id: { type: "string", minLength: 36, maxLength: 36, pattern: "^[a-f0-9-]{36}$" },
            } : {},
            required: definition.id ? ["monitor_id"] : [],
            additionalProperties: false,
        },
        handler: async (args = {}) => {
            try {
                if (!validArgs(args, definition.id)) throw new ObservationError("invalid_arguments");
                return result(await definition.run(getObserver(), args));
            } catch (error) {
                if (error instanceof ObservationError) {
                    return result({ error: error.code, message: error.message }, "rejected");
                }
                return result({ error: "observer_error", message: "The activity observer could not complete this request. No SkyN3t process was started or cancelled." }, "failure");
            }
        },
    }));
}

export function attachLifecycle(session, observer, signals = process) {
    let stopping;
    const cleanup = () => {
        if (!stopping) {
            unsubscribe();
            signals.removeListener("SIGTERM", cleanup);
            stopping = observer.shutdown();
        }
        return stopping;
    };
    const unsubscribe = session.on("session.shutdown", cleanup);
    signals.on("SIGTERM", cleanup);
    return cleanup;
}
