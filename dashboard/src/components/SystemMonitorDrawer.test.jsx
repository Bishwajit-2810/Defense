import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { vi } from 'vitest';

import SystemMonitorDrawer from './SystemMonitorDrawer.jsx';

const apiCall = vi.fn();
vi.mock('../utils/api', () => ({
  apiCall: (...a) => apiCall(...a),
  API_BASE: 'http://127.0.0.1:8001',
  getSseQueryAsync: vi.fn().mockResolvedValue('?ticket=test'),
}));

const MOCK_SYSTEM_STATS = {
  timestamp: '2026-08-21T02:30:00.000Z',
  status: 'healthy',
  host: {
    hostname: 'arch',
    os: 'Linux 7.1.4-arch1-1',
    platform: 'Linux-7.1.4-arch1-1-x86_64',
    architecture: 'x86_64',
    python_version: '3.12.12',
    uptime_seconds: 28148.9,
    uptime_human: '7h 49m 8s',
    cpu: {
      percent: 6.4,
      per_cpu_percent: [4.1, 6.1, 4.6, 8.3],
      cores_logical: 16,
      cores_physical: 8,
      load_avg: [2.08, 1.12, 0.96],
      frequency: { current_mhz: 1750.7, min_mhz: 800.0, max_mhz: 4600.0 },
      context_switches: 691225358,
    },
    memory: {
      total_human: '31.1 GB',
      used_human: '13.5 GB',
      available_human: '17.6 GB',
      free_human: '743.0 MB',
      cached_human: '18.0 GB',
      buffers_human: '748.0 KB',
      percent: 43.4,
      swap_total_human: '15.5 GB',
      swap_used_human: '2.5 GB',
      swap_percent: 16.3,
    },
    storage: {
      primary: {
        path: '/',
        total_human: '464.8 GB',
        used_human: '345.1 GB',
        free_human: '115.4 GB',
        percent: 74.9,
      },
      mounts: [
        {
          path: '/',
          total_human: '464.8 GB',
          used_human: '345.1 GB',
          free_human: '115.4 GB',
          percent: 74.9,
        },
      ],
      io: {
        read_bytes_human: '62.4 GB',
        write_bytes_human: '76.3 GB',
        read_count: 1876415,
        write_count: 6365586,
        busy_time_ms: 685396,
      },
    },
    gpu: {
      available: true,
      device_count: 2,
      driver_version: '610.43.03',
      torch_cuda_available: true,
      discrete: [
        {
          index: 0,
          name: 'NVIDIA GeForce RTX 3050 Laptop GPU',
          type: 'discrete',
          driver_version: '610.43.03',
          temperature_c: 70.0,
          utilization_gpu_percent: 0.0,
          memory_total_human: '4.0 GB',
          memory_used_human: '2.6 GB',
          memory_percent: 66.2,
          power_draw_w: 7.8,
        },
      ],
      integrated: [
        {
          card: 'card1',
          name: 'Intel Corporation TigerLake-H GT1 [UHD Graphics]',
          vendor: 'Intel',
          driver: 'i915',
          pci_slot: '0000:00:02.0',
          power_status: 'active',
          frequency: {
            cur_mhz: 350,
            boost_mhz: 1450,
          },
        },
      ],
    },
    network: {
      bytes_sent_human: '2.2 GB',
      bytes_recv_human: '3.3 GB',
      packets_sent: 3214577,
      packets_recv: 2894433,
      errors_total: 0,
      drops_total: 190,
    },
    fans: {
      available: true,
      count: 2,
      primary_rpm: 3800,
      fans: [
        { label: 'cpu_fan', current_rpm: 3800, chip: 'asus' },
        { label: 'gpu_fan', current_rpm: 3800, chip: 'asus' },
      ],
    },
  },
};

beforeEach(() => {
  apiCall.mockReset();
});

describe('SystemMonitorDrawer', () => {
  it('renders nothing when isOpen is false', () => {
    const { container } = render(<SystemMonitorDrawer isOpen={false} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('fetches and displays all 6 core hardware telemetry metrics on single page', async () => {
    apiCall.mockResolvedValue(MOCK_SYSTEM_STATS);
    render(<SystemMonitorDrawer isOpen={true} onClose={() => {}} />);

    expect(apiCall).toHaveBeenCalledWith('/v1/system/stats');
    expect(await screen.findByText('System Monitor')).toBeInTheDocument();
    expect(await screen.findByText('HEALTHY')).toBeInTheDocument();

    // 1. CPU
    expect(screen.getByText('CPU')).toBeInTheDocument();
    expect(screen.getByText(/16 Logical \/ 8 Physical Cores/i)).toBeInTheDocument();

    // 2. GPU
    expect(screen.getByText(/GPU & Graphics/i)).toBeInTheDocument();
    expect(screen.getAllByText('NVIDIA GeForce RTX 3050 Laptop GPU').length).toBeGreaterThan(0);
    expect(screen.getByText('Intel Corporation TigerLake-H GT1 [UHD Graphics]')).toBeInTheDocument();

    // 3. RAM
    expect(screen.getByText(/RAM \(Memory\)/i)).toBeInTheDocument();
    expect(screen.getByText(/13.5 GB used of 31.1 GB total/i)).toBeInTheDocument();

    // 4. Storage
    expect(screen.getByText(/Storage & Disk/i)).toBeInTheDocument();
    expect(screen.getByText(/345.1 GB used of 464.8 GB on \//i)).toBeInTheDocument();

    // 5. Network
    expect(screen.getByText(/Network Traffic/i)).toBeInTheDocument();
    expect(screen.getByText('2.2 GB')).toBeInTheDocument();
    expect(screen.getByText('3.3 GB')).toBeInTheDocument();

    // 6. Fan Speed
    expect(screen.getByText(/Fan Speed & Cooling/i)).toBeInTheDocument();
    expect(screen.getByText(/CPU_FAN/i)).toBeInTheDocument();
    expect(screen.getByText(/GPU_FAN/i)).toBeInTheDocument();
  });

  it('renders error message when telemetry request fails', async () => {
    apiCall.mockRejectedValue(new Error('Backend connection refused'));
    render(<SystemMonitorDrawer isOpen={true} onClose={() => {}} />);

    expect(await screen.findByText(/Backend connection refused/)).toBeInTheDocument();
  });

  it('calls onClose when close button is clicked', async () => {
    apiCall.mockResolvedValue(MOCK_SYSTEM_STATS);
    const handleClose = vi.fn();
    render(<SystemMonitorDrawer isOpen={true} onClose={handleClose} />);

    const closeBtn = await screen.findByLabelText('Close monitor');
    fireEvent.click(closeBtn);
    expect(handleClose).toHaveBeenCalled();
  });

  it('calls onClose when ESC key is pressed', async () => {
    apiCall.mockResolvedValue(MOCK_SYSTEM_STATS);
    const handleClose = vi.fn();
    render(<SystemMonitorDrawer isOpen={true} onClose={handleClose} />);

    await screen.findByText('System Monitor');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(handleClose).toHaveBeenCalled();
  });

  it('displays real-time live digital clock and live stream beacon', async () => {
    apiCall.mockResolvedValue(MOCK_SYSTEM_STATS);
    render(<SystemMonitorDrawer isOpen={true} onClose={() => {}} />);

    await screen.findByText('System Monitor');
    expect(screen.getByText(/LIVE REAL-TIME STREAM/i)).toBeInTheDocument();
    expect(screen.getByText(/· LIVE/i)).toBeInTheDocument();
  });

  it('copies snapshot to clipboard', async () => {
    apiCall.mockResolvedValue(MOCK_SYSTEM_STATS);
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(),
      },
    });

    render(<SystemMonitorDrawer isOpen={true} onClose={() => {}} />);
    await screen.findByText('System Monitor');

    const copyBtn = screen.getByTitle('Copy JSON snapshot');
    fireEvent.click(copyBtn);
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });
});
