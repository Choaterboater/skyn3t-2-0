import React, { Suspense, useEffect, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill } from "../components/ui.jsx";

// A lightweight stand-in "brain": an icosahedron core wrapped in a wireframe
// shell with orbiting synapse nodes. Pure r3f/three primitives — no asset
// loading, so it builds and runs without external models.
// Themed in SYNAPSE violet (the mind) with EMBER accents (active thought).
function BrainMesh({ activity = 0.3 }) {
  const core = useRef();
  const shell = useRef();

  useFrame((state, delta) => {
    if (core.current) core.current.rotation.y += delta * 0.3;
    if (shell.current) {
      shell.current.rotation.y -= delta * 0.15;
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

  const glow = 0.4 + Math.min(activity, 1) * 0.6;

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

  const activity =
    (data?.activity != null ? Number(data.activity) : 0) +
    Math.min(learningEvents / 20, 1);

  const hot = activity > 0.5;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · The Mind"
        title="Brain"
        sub="Knowledge graph activity. The core glows as the swarm learns — synapses fire with every lesson captured."
        actions={
          <Pill tone={hot ? "ember" : "synapse"}>
            {hot ? "thinking" : "at rest"}
          </Pill>
        }
      />

      <div className="mb-6 grid grid-cols-3 gap-4">
        <Stat
          label="Documents"
          value={data?.documents ?? "—"}
          tone="synapse"
        />
        <Stat
          label="Lessons"
          value={data?.lessons ?? "—"}
          tone="synapse"
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
        <div className="relative h-[480px] bg-void">
          <div className="pointer-events-none absolute left-1/2 top-1/2 h-72 w-72 -translate-x-1/2 -translate-y-1/2 rounded-full bg-synapse/10 blur-3xl" />
          <Canvas
            camera={{ position: [0, 0, 6], fov: 50 }}
            gl={{ powerPreference: "low-power" }}
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
              <BrainMesh activity={activity} />
            </Suspense>
          </Canvas>
        </div>
      </Panel>
    </div>
  );
}
