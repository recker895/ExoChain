"use client";
import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection, Feature, Geometry } from "geojson";
import type { Vessel, Route, Segment } from "./types";

type Props = {
  vessels: Vessel[];
  routes: Route[];
  recommended: Set<string>;
  onVessel: (v: Vessel) => void;
  onSegment: (s: Segment) => void;
  ports: Record<string, unknown>[];
  risks?: {entity_id: string; score?: number | null}[];
  trajectory?: Vessel["history"];
};

/** Display consecutive points in one world copy, rather than across the map. */
export function unwrapRouteCoordinates(
  points: readonly { longitude: number; latitude: number }[], anchor?: number,
): [number, number][] {
  let previous = anchor;
  return points.map(point => {
    let longitude = point.longitude;
    if (previous !== undefined) {
      while (longitude - previous > 180) longitude -= 360;
      while (longitude - previous < -180) longitude += 360;
    }
    previous = longitude;
    return [longitude, point.latitude];
  });
}

export default function OperationsMap({
  vessels,
  routes,
  recommended,
  onVessel,
  onSegment,
  ports,
  risks = [],
  trajectory = [],
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const props = useRef({
    vessels,
    routes,
    recommended,
    onVessel,
    onSegment,
    ports,
  });
  const [ready, setReady] = useState(false);
  const [renderedVessels, setRenderedVessels] = useState(0);
  const [error, setError] = useState("");
  const fitted = useRef("");
  useEffect(() => {
    props.current = {
      vessels,
      routes,
      recommended,
      onVessel,
      onSegment,
      ports,
    };
  }, [vessels, routes, recommended, onVessel, onSegment, ports]);
  useEffect(() => {
    if (!container.current) return;
    // Fast refresh/Strict Mode can retain state while rebuilding the map.
    // The old "ready" value must not authorize updates to a new empty style.
    setReady(false);
    fitted.current = "";
    maplibregl.setWorkerUrl("/maplibre-gl-worker.mjs");
    const instance = new maplibregl.Map({
      container: container.current,
      center: [40, 18],
      zoom: 1.35,
      minZoom: 1,
      attributionControl: { compact: true },
      style: process.env.NEXT_PUBLIC_MAP_STYLE_URL || {
        version: 8,
        sources: {
          basemap: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [
          {
            id: "base",
            type: "raster",
            source: "basemap",
            paint: {
              "raster-saturation": -1,
              "raster-brightness-max": 0.3,
              "raster-brightness-min": 0.03,
              "raster-contrast": 0.35,
            },
          },
        ],
      },
    });
    map.current = instance;
    instance.on("idle", () => {
      if (instance.getLayer("vessels")) setRenderedVessels(new Set(instance.queryRenderedFeatures({layers: ["vessels"]}).map(f => f.properties.id)).size);
    });
    instance.addControl(
      new maplibregl.NavigationControl({ showCompass: false }),
      "top-right",
    );
    instance.on("style.load", () => {
      for (const id of ["vessels", "routes", "ports", "endpoints", "risks", "trajectory"])
        instance.addSource(id, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
      instance.addLayer({id: "observed-risk", type: "circle", source: "risks", paint: {"circle-color": "#e7a765", "circle-radius": ["interpolate", ["linear"], ["get", "score"], 0, 6, 1, 28], "circle-opacity": 0.22, "circle-stroke-color": "#e7a765", "circle-stroke-width": 1}});
      instance.addLayer({id: "observed-trajectory", type: "line", source: "trajectory", paint: {"line-color": "#9db8f5", "line-width": 2, "line-dasharray": [2, 2]}});
      instance.addLayer({
        id: "alternatives",
        type: "line",
        source: "routes",
        filter: ["==", ["get", "recommended"], false],
        paint: {
          "line-color": "#8397aa",
          "line-width": 2,
          "line-dasharray": [3, 3],
          "line-opacity": 0.8,
        },
      });
      instance.addLayer({
        id: "recommended",
        type: "line",
        source: "routes",
        filter: ["==", ["get", "recommended"], true],
        paint: { "line-color": "#63e2ca", "line-width": 4 },
      });
      instance.addLayer({
        id: "ports",
        type: "circle",
        source: "ports",
        paint: {
          "circle-color": "#a3b7cd",
          "circle-radius": 4,
          "circle-stroke-color": "#111c2a",
          "circle-stroke-width": 2,
        },
      });
      instance.addLayer({
        id: "vessels",
        type: "circle",
        source: "vessels",
        paint: {
          "circle-color": [
            "case",
            ["==", ["get", "quality"], "VALID"],
            "#64ddc7",
            "#dfa866",
          ],
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 3, 8, 6],
          "circle-stroke-color": "#0e202b",
          "circle-stroke-width": 1.5,
        },
      });
      instance.addLayer({
        id: "endpoints",
        type: "circle",
        source: "endpoints",
        paint: {
          "circle-color": "#e5f7f1",
          "circle-radius": 6,
          "circle-stroke-color": "#42bda3",
          "circle-stroke-width": 3,
        },
      });
      instance.on("click", "vessels", (e) => {
        const id = e.features?.[0]?.properties?.id;
        const v = props.current.vessels.find((v) => v.mmsi === id);
        if (v) props.current.onVessel(v);
      });
      for (const layer of ["recommended", "alternatives"])
        instance.on("click", layer, (e) => {
          const id = e.features?.[0]?.properties?.segment;
          const segment = props.current.routes
            .flatMap((r) => r.segments || [])
            .find((s) => s.id === id);
          if (segment) props.current.onSegment(segment);
        });
      instance.on("click", "ports", (e) => {
        const element = document.createElement("div");
        element.textContent = String(
          e.features?.[0]?.properties?.name || "Port",
        );
        new maplibregl.Popup()
          .setLngLat(e.lngLat)
          .setDOMContent(element)
          .addTo(instance);
      });
      for (const layer of ["vessels", "recommended", "alternatives", "ports"]) {
        instance.on("mouseenter", layer, () => {
          instance.getCanvas().style.cursor = "pointer";
        });
        instance.on("mouseleave", layer, () => {
          instance.getCanvas().style.cursor = "";
        });
      }
      setReady(true);
    });
    instance.on("error", () =>
      setError(
        "Some basemap tiles are unavailable. Evidence layers remain independent of the basemap.",
      ),
    );
    return () => {
      instance.remove();
      map.current = null;
    };
  }, []);
  useEffect(() => {
    if (!ready || !map.current) return;
    const instance = map.current;
    if (!instance.getSource("routes")) return;
    const update = (id: string, features: Feature<Geometry>[]) => {
      const source = instance.getSource<maplibregl.GeoJSONSource>(id);
      if (!source) return;
      source.setData({
        type: "FeatureCollection",
        features,
      } as FeatureCollection);
    };
    update(
      "vessels",
      vessels
        .filter(
          (v) =>
            Number.isFinite(v.position?.latitude) &&
            Number.isFinite(v.position?.longitude),
        )
        .map((v) => ({
          type: "Feature",
          properties: { id: v.mmsi, quality: v.quality },
          geometry: {
            type: "Point",
            coordinates: [v.position.longitude, v.position.latitude],
          },
        })),
    );
    update("risks", vessels.flatMap(v => {
      const score = risks.find(r => r.entity_id === v.mmsi)?.score;
      return score == null || !Number.isFinite(score) ? [] : [{type: "Feature" as const, properties: {score}, geometry: {type: "Point" as const, coordinates: [v.position.longitude, v.position.latitude]}}];
    }));
    const points = trajectory.filter(p => Number.isFinite(p.latitude) && Number.isFinite(p.longitude));
    update("trajectory", points.length < 2 ? [] : [{type: "Feature", properties: {}, geometry: {type: "LineString", coordinates: unwrapRouteCoordinates(points)}}]);
    update(
      "routes",
      routes.flatMap((r) => {
        const continuous = unwrapRouteCoordinates(r.geometry);
        return (r.segments?.length ? r.segments : [{ id: r.candidate_id, geometry: r.geometry }]).map((s) => {
          const first = s.geometry[0];
          const index = r.geometry.findIndex(p => first && p.longitude === first.longitude && p.latitude === first.latitude);
          return {
            type: "Feature" as const,
            properties: {
              id: r.candidate_id,
              segment: s.id,
              recommended: recommended.has(r.candidate_id),
            },
            geometry: {
              type: "LineString" as const,
              coordinates: unwrapRouteCoordinates(s.geometry, continuous[index]?.[0]),
            },
          };
        });
      }),
    );
    update(
      "ports",
      ports
        .filter(
          (p) =>
            typeof p.latitude === "number" && typeof p.longitude === "number",
        )
        .map((p) => ({
          type: "Feature",
          properties: { name: p.name },
          geometry: {
            type: "Point",
            coordinates: [Number(p.longitude), Number(p.latitude)],
          },
        })),
    );
    update(
      "endpoints",
      routes
        .filter((r) => recommended.has(r.candidate_id))
        .flatMap((r) => {
          const continuous = unwrapRouteCoordinates(r.geometry);
          return [continuous[0], continuous.at(-1)].filter(Boolean).map((p) => ({
            type: "Feature" as const,
            properties: {},
            geometry: {
              type: "Point" as const,
              coordinates: p!,
            },
          }));
        }),
    );
    const selected = routes.find((r) => recommended.has(r.candidate_id))
      || routes.find((r) => r.candidate_id === "geographic-planning-preview");
    if (!selected && !fitted.current && vessels.length) {
      const bounds = new maplibregl.LngLatBounds();
      vessels.filter(v => Number.isFinite(v.position?.longitude) && Number.isFinite(v.position?.latitude)).forEach(v => bounds.extend([v.position.longitude, v.position.latitude]));
      if (!bounds.isEmpty()) {
        instance.fitBounds(bounds, {padding: 55, maxZoom: 6, duration: 0});
        fitted.current = "observed-fleet";
      }
    }
    if (selected && fitted.current !== selected.candidate_id) {
      const bounds = new maplibregl.LngLatBounds();
      unwrapRouteCoordinates(selected.geometry).forEach((p) =>
        bounds.extend(p),
      );
      instance.fitBounds(bounds, { padding: 70, maxZoom: 8 });
      fitted.current = selected.candidate_id;
    }
  }, [ready, vessels, routes, recommended, ports, risks, trajectory]);
  return (
    <div className="map-wrap" data-rendered-vessels={renderedVessels}>
      <div ref={container} className="map-canvas" />
      {error && <div className="map-error">{error}</div>}
      <div className="map-attribution-note">
        Geography: community basemap · Operations: source observations
      </div>
    </div>
  );
}
