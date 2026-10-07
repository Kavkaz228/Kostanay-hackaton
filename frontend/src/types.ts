export type Status = 'running' | 'stopped' | 'starved' | 'blocked';
export type Severity = 'warning' | 'critical' | 'info';
export interface Station {
  id: string; name: string; order: number; cycle_seconds: number; capacity: number;
  buffer_capacity: number; defect_rate: number; speed_factor: number; manual_stop: boolean;
  status: Status; queue: number; in_process: number | null; completed: number; rejected: number;
  utilization: number | null; downtime_seconds: number; throughput: number | null; progress: number | null;
}
export interface Prediction {
  station_id: string; station_name: string; type: string; severity: Severity;
  title: string; description: string; eta_minutes: number | null;
}
export interface TwinState {
  telemetry_available?: {production: boolean; robots: boolean};
  telemetry_kind?: 'robots'; robots?: RobotObservation[]; observation_count?: number; observation_start?: string;
  run_id: string; source: 'simulation' | 'telemetry'; updated_at: string; running: boolean;
  speed: number; sim_time: number; shift_duration: number; shift_plan: number;
  stations: Station[];
  metrics: {produced: number | null; good: number | null; rejected: number | null; wip: number | null; throughput: number | null;
    availability: number | null; performance: number | null; quality: number | null; oee: number | null;
    plan_progress: number | null; forecast_good: number | null};
  predictions: Prediction[]; model: {name: string; description: string};
  telemetry_updated_at: string | null;
}
export interface RobotObservation {
  timestamp: string; robot_id: string; line_section: string; cycle_status: string; error_code: string;
  joint_temperature_c: number | null; vibration_mm_s: number | null; hydraulic_pressure_bar: number | null;
  pneumatic_pressure: number | null; pneumatic_pressure_bar: number | null;
}
export interface HistoryPoint {
  sim_time: number; produced: number; rejected: number; throughput: number; wip: number;
  availability: number; quality: number;
}
export interface Incident {
  id: string; station_id: string; station_name: string; severity: Severity; kind: string;
  title: string; description: string; created_at: string; sim_time: number;
  status: 'open' | 'acknowledged' | 'resolved'; acknowledged_at?: string; resolved_at?: string;
}
export interface StationChange {
  station_id: string; cycle_seconds?: number; capacity?: number; buffer_capacity?: number;
  defect_rate?: number; speed_factor?: number; manual_stop?: boolean;
}
export interface Outcome {good: number; rejected: number; wip: number; downtime_minutes: number}
export interface Scenario {
  id: string; name: string; created_at: string; horizon_minutes: number;
  changes: StationChange[]; baseline: Outcome; variant: Outcome; delta: Outcome;
  timeline: {minute: number; baseline_good: number; variant_good: number}[];
  explanation: string;
}

