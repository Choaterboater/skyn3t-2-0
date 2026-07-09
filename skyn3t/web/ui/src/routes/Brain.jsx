import React, { Suspense, useEffect, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill } from "../components/ui.jsx";

// How many lessons captured this session light the brain to full heat. Small,
// so a handful of lessons visibly warms the core rather than staying flat.
const LESSONS_TO_FULL = 8;

// A lightweight stand-in "brain": an icosahedron core wrapped in a wireframe
// shell with orbiting synapse nodes. Pure r3f/three primitives — no asset
// loading, so it builds and runs without external models.
// Themed in SYNAPSE violet (the mind) with EMBER accents (active thought).
function BrainMesh({ activity = 0, reduced = false }) {
  const core = useRef();
  const shell = useRef();

  useFrame((state, delta) => {
    if (reduced) return; // honor prefers-reduced-motion — hold still
    // Spins faster the more it's learning, so motion itself reads as thought.
    const spin = 0.18 + activity * 0.55;
    if (core.current) core.current.rotation.y += delta * spin;
    if (shell.current) {
      shell.current.rotation.y -= delta * spin * 0.5;
      shell.current.rotation.x += delta * 0.05;
    }
  });

  const synapses = [];
  const count = 14;
  for (let i = 0; i < count; i++) {
    const t = (i / count) * Math.PI * 2;
    const r = 2.1;
    synapses.push([
      Math.cos(t) * r,
      Math.sin(t * 1.7) * 0.9,
      Math.sin(t) * r,
    ]);
  }

  const glow = 0.35 + Math.min(activity, 1) * 0.65;

  return (
    <group>
      <mesh ref={core}>
        <icosahedronGeometry args={[1.2, 1]} />
        <meshStandardMaterial
          color="#A688FF"
          emissive="#A688FF"
          emissiveIntensity={glow}
          roughness={0.3}
          metalness={0.5}
        />
      </mesh>
      <mesh ref={shell}>
        <icosahedronGeometry args={[1.9, 1]} />
        <meshStandardMaterial color="#A688FF" wireframe transparent opacity={0.25} />
      </mesh>
      {synapses.map((pos, i) => (
        <mesh key={i} position={pos}>
          <sphereGeometry args={[0.07, 12, 12]} />
          <meshStandardMaterial
            color={i % 3 === 0 ? "#FF6A3D" : "#C9B6FF"}
            emissive={i % 3 === 0 ? "#FF6A3D" : "#A688FF"}
            emissiveIntensity={glow}
          />
        </mesh>
      ))}
    </group>
  );
}

export default function Brain({ stream }) {
  // Hold the WebGL renderer so we can free its GPU context on unmount.
  const glRef = useRef(null);

  // Honor prefers-reduced-motion: the 3D spin is JS-driven (useFrame), so the
  // global CSS reduced-motion rule can't reach it — gate it here instead.
  const [reduced] = useState(
    () =>
      typeof window !== "undefined" &&
      !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  );

  // Free the context when leaving the page. Without this, each visit leaks a
  // WebGL context; browsers cap them (~16) and force-lose old ones, which spams
  // "THREE.WebGLRenderer: Context Lost".
  useEffect(() => {
    return () => {
      const gl = glRef.current;
      if (gl) {
        try {
          gl.forceContextLoss();
          gl.dispose();
        } catch (_) {
          /* best-effort */
        }
      }
      glRef.current = null;
    };
  }, []);

  // Drive the glow from recent knowledge/learning events if the API is present.
  const { data } = useQuery({
    queryKey: ["brain"],
    queryFn: queryFn("/brain"),
    retry: 0,
  });

  const learningEvents = (stream?.events || []).filter((e) =>
    // EventType.value is lowercase-dotted on the wire, not the enum NAME.
    ["knowledge.updated", "insight.published", "lesson.captured"].includes(
      e.type
    )
  ).length;

  // `data.activity` from /brain is a *cumulative* event counter (published_count),
  // not a 0–1 level — feeding it to the glow pinned the core at max forever, so
  // the page never reacted. Drive the glow from live learning this session
  // instead, normalized to 0–1, so an idle brain rests and lessons heat it.
  const activity = Math.min(learningEvents / LESSONS_TO_FULL, 1);
  const hot = learningEvents > 0;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · The Mind"
        title="Brain"
        sub="Knowledge graph activity. The core glows as the swarm learns — synapses fire with every lesson captured."
        actions={
          <Pill tone={hot ? "ember" : "synapse"}>
            {hot ? "learning" : "at rest"}
          </Pill>
        }
      />

      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Documents"
          value={data?.documents ?? "—"}
          tone="synapse"
          hint="builds remembered"
        />
        <Stat
          label="Lessons"
          value={data?.lessons ?? "—"}
          tone="synapse"
          hint="learned the hard way"
        />
        <Stat
          label="Agents"
          value={data?.agents ?? "—"}
          tone="synapse"
          hint="minds in the swarm"
        />
        <Stat
          label="Learning events"
          value={learningEvents}
          tone={learningEvents ? "ember" : "bone"}
          hint="this session"
        />
      </div>

      <Panel className="overflow-hidden" glow={hot}>
        <PanelHead
          label="Knowledge graph"
          right={
            <span className="font-mono text-[11px] text-synapse">
              activity · {activity.toFixed(2)}
            </span>
          }
        />
        <div className="relative h-[360px] bg-void sm:h-[480px]">
          <div className="pointer-events-none absolute left-1/2 top-1/2 h-72 w-72 -translate-x-1/2 -translate-y-1/2 rounded-full bg-synapse/10 blur-3xl" />
          <Canvas
            aria-label="Animated knowledge graph activity"
            role="img"
            camera={{ position: [0, 0, 6], fov: 50 }}
            gl={{ powerPreference: "low-power" }}
            fallback={
              <div role="status" className="grid h-full place-items-center font-mono text-xs text-ash">
                3D knowledge view is unavailable in this browser.
              </div>
            }
            onCreated={({ gl }) => {
              glRef.current = gl;
              // Swallow context-loss so a transient GPU hiccup doesn't throw.
              gl.domElement.addEventListener(
                "webglcontextlost",
                (e) => e.preventDefault(),
                false
              );
            }}
          >
            <ambientLight intensity={0.4} />
            <pointLight position={[5, 5, 5]} intensity={1.2} color="#A688FF" />
            <pointLight position={[-5, -3, -5]} intensity={0.6} color="#FF6A3D" />
            <Suspense fallback={null}>
              <BrainMesh activity={activity} reduced={reduced} />
            </Suspense>
          </Canvas>
        </div>
        {/* Give both states direction: tell the reader what the glow means and,
            when idle, what would make it move — an empty state as invitation. */}
        <div className="border-t border-hairline px-4 py-2.5 font-mono text-[11px] text-ash">
          {hot ? (
            <>
              Core heating —{" "}
              <span className="text-ember">{learningEvents}</span> lesson
              {learningEvents !== 1 ? "s" : ""} captured this session.
            </>
          ) : (
            "At rest. The core warms as the swarm captures lessons and updates the knowledge graph."
          )}
        </div>
      </Panel>
    </div>
  );
}
