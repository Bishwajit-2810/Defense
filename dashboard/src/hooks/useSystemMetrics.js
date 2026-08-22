import { useState, useEffect, useRef, useCallback } from 'react';
import { apiCall, API_BASE, getSseQueryAsync } from '../utils/api';

/**
 * Extracts and normalizes CPU, GPU, and RAM telemetry metrics.
 */
export function extractSystemMetrics(data) {
  if (!data) {
    return {
      cpu: 0,
      gpu: null,
      gpuAvailable: false,
      ram: 0,
      ramUsed: '',
      ramTotal: '',
      status: 'offline',
      raw: null,
    };
  }

  // 1. CPU percentage
  const cpuRaw = data?.host?.cpu?.percent ?? data?.cpu?.percent ?? 0;
  const cpu = typeof cpuRaw === 'number' ? Math.round(cpuRaw * 10) / 10 : Number(cpuRaw) || 0;

  // 2. GPU percentage & availability
  let gpu = null;
  let gpuAvailable = false;
  const gpuObj = data?.gpu || data?.host?.gpu;
  if (gpuObj) {
    gpuAvailable = !!gpuObj.available || (Array.isArray(gpuObj.devices) && gpuObj.devices.length > 0) || (Array.isArray(gpuObj.discrete) && gpuObj.discrete.length > 0);
    if (Array.isArray(gpuObj.discrete) && gpuObj.discrete.length > 0) {
      const d = gpuObj.discrete[0];
      const gVal = d?.utilization_gpu_percent;
      gpu = typeof gVal === 'number' ? Math.round(gVal * 10) / 10 : 0;
    } else if (Array.isArray(gpuObj.devices) && gpuObj.devices.length > 0) {
      const d = gpuObj.devices[0];
      const gVal = d?.utilization_gpu_percent;
      gpu = typeof gVal === 'number' ? Math.round(gVal * 10) / 10 : 0;
    } else if (Array.isArray(gpuObj.integrated) && gpuObj.integrated.length > 0) {
      const i = gpuObj.integrated[0];
      const gVal = i?.utilization_percent;
      gpu = typeof gVal === 'number' ? Math.round(gVal * 10) / 10 : 0;
    } else if (gpuAvailable) {
      gpu = 0;
    }
  }

  // 3. RAM percentage and human readouts
  const mem = data?.host?.memory || data?.memory || {};
  const memRaw = mem.percent ?? 0;
  const ram = typeof memRaw === 'number' ? Math.round(memRaw * 10) / 10 : Number(memRaw) || 0;
  const ramUsed = mem.used_human || '';
  const ramTotal = mem.total_human || '';

  return {
    cpu,
    gpu,
    gpuAvailable,
    ram,
    ramUsed,
    ramTotal,
    status: data?.status || 'healthy',
    raw: data,
  };
}

/**
 * Custom React hook for live real-time CPU, GPU, and RAM telemetry streaming and polling.
 */
export function useSystemMetrics({ enabled = true, pollInterval = 3000 } = {}) {
  const [metrics, setMetrics] = useState(() => extractSystemMetrics(null));
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const eventSourceRef = useRef(null);
  const pollTimerRef = useRef(null);
  const isMountedRef = useRef(true);

  const handleData = useCallback((data) => {
    if (!isMountedRef.current) return;
    const extracted = extractSystemMetrics(data);
    setMetrics(extracted);
    setIsLive(true);
    setError(null);
  }, []);

  const fetchSnapshot = useCallback(async (isSilent = false) => {
    if (!isSilent) setLoading(true);
    try {
      const data = await apiCall('/v1/system/stats');
      handleData(data);
    } catch (err) {
      if (isMountedRef.current) {
        setError(err.message || 'Telemetry unavailable');
        setIsLive(false);
      }
    } finally {
      if (!isSilent && isMountedRef.current) {
        setLoading(false);
      }
    }
  }, [handleData]);

  useEffect(() => {
    isMountedRef.current = true;

    if (!enabled) {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      setIsLive(false);
      return;
    }

    // Initial fetch
    fetchSnapshot(false);

    let es = null;

    async function initStream() {
      if (typeof EventSource === 'undefined') {
        // Fallback directly to polling if EventSource is not supported (e.g. testing)
        pollTimerRef.current = setInterval(() => {
          fetchSnapshot(true);
        }, pollInterval);
        return;
      }

      try {
        const sseQuery = await getSseQueryAsync();
        if (!isMountedRef.current) return;

        es = new EventSource(`${API_BASE}/v1/system/stream${sseQuery}`);
        eventSourceRef.current = es;

        es.addEventListener('stats', (e) => {
          if (!isMountedRef.current) return;
          try {
            const parsed = JSON.parse(e.data);
            handleData(parsed);
          } catch (err) {
            console.error('Failed to parse SSE stats:', err);
          }
        });

        es.onopen = () => {
          if (isMountedRef.current) setIsLive(true);
        };

        es.onerror = () => {
          if (!isMountedRef.current) return;
          setIsLive(false);
          // Fallback to polling when SSE disconnects
          if (!pollTimerRef.current) {
            pollTimerRef.current = setInterval(() => {
              fetchSnapshot(true);
            }, pollInterval);
          }
        };
      } catch {
        if (isMountedRef.current && !pollTimerRef.current) {
          pollTimerRef.current = setInterval(() => {
            fetchSnapshot(true);
          }, pollInterval);
        }
      }
    }

    initStream();

    return () => {
      isMountedRef.current = false;
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [enabled, pollInterval, fetchSnapshot, handleData]);

  return {
    metrics,
    isLive,
    loading,
    error,
    refresh: () => fetchSnapshot(false),
  };
}

export default useSystemMetrics;
