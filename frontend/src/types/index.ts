// 类型定义

export interface RevisionChange {
  op: string
  day_index?: number | null
  title: string
  detail?: string
}

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  route_entry?: { poi_id: string; name: string; location: Location; source: string } | null
  route_exit?: { poi_id: string; name: string; location: Location; source: string } | null
  visit_mode?: 'classic' | 'deep'
  duration_reason?: string
  duration_source?: 'ai_estimate' | 'user_confirmed'
  suitable_time?: 'day' | 'evening' | 'night' | 'any' | 'unknown'
  match_evidence?: { status?: string; method?: string; input_name?: string; matched_name?: string; recommendation_evidence_status?: 'quote_verified' | 'source_context_verified' | 'unverified' }
  parent_poi_id?: string
  stable_id?: string
  amap_average_cost?: number | null
  advisory?: AttractionAdvisory | null
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  recommendation_sources?: string[]
  photos?: string[]
  poi_id?: string
  ticket_price?: number | null
  ticket_price_source?: string
  ticket_price_note?: string
  opening_time?: string
  closing_time?: string
  last_entry_time?: string
  closed_weekdays?: number[]
}

export interface AttractionAdvisory {
  reservation_channel?: string
  reservation_url?: string
  opening_hours_note?: string
  opening_checked_at?: string | null
  opening_expires_at?: string | null
  refresh_errors?: string[]
  reservation_required: 'true' | 'false' | 'unknown'
  closure_rules: string[]
  pitfall_tips: string[]
  source_refs: string[]
  fact_status: 'verified' | 'estimated' | 'unverified'
  verified_at?: string | null
  expires_at?: string | null
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  poi_id?: string
  price_policy?: 'auto' | 'budget_only'
  price_source?: string
  price_note?: string
  amap_average_cost?: number | null
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface DailyRouteSummary {
  distance_before_km: number
  distance_after_km: number
  estimated_travel_minutes: number
  saved_percent: number
  method: string
  start_anchor: string
  end_anchor: string
  start_transfer_km: number
  end_transfer_km: number
}

export type TransportMode = 'walking' | 'cycling' | 'driving' | 'transit'
export interface ScheduleItem {
  route_origin?: Location
  route_destination?: Location
  route_reference_time?: string
  selected_route_mode?: TransportMode | ''
  route_leg?: { mode: string; distance_km: number; duration_minutes: number; cost: number; cost_unit: 'person' | 'vehicle'; source: string; is_estimated: boolean; cost_is_estimated: boolean; cost_source: string; checked_at: string; note: string }
  start_time: string
  end_time: string
  item_type: 'meal' | 'transport' | 'attraction' | 'hotel' | 'break'
  title: string
  description: string
  address: string
  related_index?: number
  duration_minutes: number
  notes: string
  stable_id: string
  start_day_offset: number
  end_day_offset: number
}

export interface OptimizationSummary {
  method: string
  total_distance_before_km: number
  total_distance_after_km: number
  saved_distance_km: number
  saved_percent: number
  optimized_days: number
  note: string
}

export interface Budget {
  unknown_ticket_count?: number
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
  intercity_transportation: number
  local_transportation: number
  per_person: number
  budget_limit?: number
  over_budget: boolean
  calculation_note: string
}

export interface DataSource {
  name: string
  category: 'poi' | 'weather' | 'hotel' | 'transportation' | 'generation' | 'fallback'
  source_type: 'live' | 'estimated' | 'generated' | 'static'
  note: string
}

export interface DayPlan {
  schedule_mode?: 'duration' | 'timed'
  visit_minutes?: number
  transport_minutes?: number
  total_minutes?: number
  timing_notes?: string[]
  arrangement_reason?: string
  single_place_reason?: string
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
  route_summary?: DailyRouteSummary
  theme: string
  schedule: ScheduleItem[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
  data_quality: 'live' | 'mixed' | 'fallback'
  is_fallback: boolean
  warnings: string[]
  data_sources: DataSource[]
  revision_diff?: RevisionChange[]
  revision_status?: 'draft_ready' | 'applied' | 'rejected' | 'unchanged' | 'clarify_needed' | null
  revision_message?: string
  optimization_summary?: OptimizationSummary
  planning_iterations: number
  intercity_journey?: IntercityJourney
  return_journey?: IntercityJourney
}

export interface IntercityJourney {
  mode: string
  leg: 'outbound' | 'return'
  name: string
  departure_date: string
  arrival_date: string
  departure_time: string
  arrival_time: string
  departure_station: string
  arrival_station: string
  check_in_buffer_minutes: number
  station_transfer_minutes: number
  arrival_transfer_minutes: number
  is_estimated: boolean
  source: string
}

export interface TripFormData {
  origin_city: string
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  trip_type: 'round_trip' | 'one_way'
  departure_time?: string
  arrival_time?: string
  return_departure_time?: string
  return_arrival_time?: string
  accommodation: string
  preferences: string[]
  free_text_input: string
  conversation_id?: string | null
  conversation_context?: string
  travelers: number
  rooms: number
  budget_limit?: number
  optimization_goal: 'balanced' | 'economy' | 'fastest'
  pace: 'relaxed' | 'balanced' | 'intensive'
  // 攻略导入确认后的候选地点：随请求进入规划，必去/想去优先安排
  candidate_places?: CandidatePlace[]
  // 生成前先比较 2-3 个行程方向（需要后端 Redis/Checkpointer 支持 interrupt）
  ask_route_options?: boolean
}

export interface PlannerChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface PlannerChatResponse {
  conversation_summary?: import('../services/conversations').ConversationSummary | null
  reply: string
  suggestions: string[]
  draft: Partial<TripFormData> & { preferences_confirmed?: boolean }
  ready: boolean
  request?: TripFormData | null
}

export type PlanningJobStatus = 'queued' | 'running' | 'retrying' | 'waiting_input' | 'cancelling' | 'cancelled' | 'completed' | 'failed'

// interrupt 载荷：需要用户确认的问题（当前仅预算确认 budget_confirm 类型）
export interface WaitingInputOption {
  value: string
  label: string
}

export interface WaitingInputPayload {
  type: string
  question: string
  options: WaitingInputOption[]
  over_budget_amount?: number
  budget_limit?: number
  current_accommodation?: string
}

export interface PlanningJobResponse {
  trace_id?: string
  worker_id?: string
  run_id?: string
  heartbeat_at?: string | null
  recovery_count?: number
  diagnostics?: { services?: Record<string, {started:number; completed:number; failed:number; hits:number; misses:number; duration_ms:number; retrying?:number; degraded?:number}>; active_calls?: Record<string,{service:string; operation:string; timestamp:string}>; events?: Record<string,any>[] }
  job_id: string
  status: PlanningJobStatus
  progress: number
  stage: string
  message: string
  data?: TripPlan
  error?: string
  agents: Record<string, AgentExecutionState>
  metrics: PlanningMetrics
  created_at?: string | null
  started_at?: string | null
  updated_at?: string | null
  waiting_input?: WaitingInputPayload | null
}

export interface AgentExecutionState {
  status: 'running' | 'completed' | 'degraded'
  message: string
  duration_ms?: number
}

export interface PlanningMetrics {
  queue_wait_ms: number
  total_duration_ms?: number
  agent_duration_ms: Record<string, number>
  degraded_agents: string[]
  stage_duration_ms?: Record<string, number>
  current_stage?: string
  stage_started_at?: string | null
  stage_elapsed_ms?: number
  stage_visits?: Record<string, number>
  retry_count?: number
  elapsed_ms?: number | null
  idle_ms?: number
  waiting_hint?: string
}

export interface PlanningProgressEvent {
  sequence: number
  progress: number
  stage: string
  message: string
  status: PlanningJobStatus
  timestamp: string
  agent?: 'attractions' | 'weather' | 'hotels' | 'transportation'
  agent_status?: 'running' | 'completed' | 'degraded'
  duration_ms?: number
}

export interface TripRevisionRequest {
  trip_request: TripFormData
  plan: TripPlan
  instruction: string
  // 乐观锁：行程库当前版本号；未保存的会话快照场景不传（后端不校验）
  base_version?: number
}

export interface TripVersionSummary {
  version: number
  change_note: string
  created_at: string
}

export interface SavedTripSummary {
  id: string
  title: string
  city: string
  start_date: string
  end_date: string
  days: number
  estimated_budget?: number
  data_quality: string
  current_version: number
  created_at: string
  updated_at: string
  // 行程封面图（首个带图景点的图片，无图时前端用城市色兜底）
  cover_image_url?: string | null
}

export interface SavedTripDetail extends SavedTripSummary {
  request: TripFormData
  plan: TripPlan
  versions: TripVersionSummary[]
}

export interface UserPreferences {
  revision?: number
  origin_city: string
  transportation: string
  accommodation: string
  preferences: string[]
  travelers: number
  rooms: number
  budget_limit?: number
  optimization_goal: 'balanced' | 'economy' | 'fastest'
  pace: 'relaxed' | 'balanced' | 'intensive'
}

export interface TripProgressItem {
  revision?: string | null
  item_key: string
  completed: boolean
  completed_at?: string
}

export interface TripProgressResponse {
  trip_id: string
  items: TripProgressItem[]
}

export interface TransportationOption {
  type: 'train' | 'high_speed_train' | 'plane' | 'bus'
  name: string
  price: number
  duration: string
  departure_time?: string
  arrival_time?: string
  departure_station?: string
  arrival_station?: string
  description?: string
  is_estimated: boolean
  source: string
}

// ---------------------------------------------------------------------------
// 攻略导入（GuideFusionGraph 前半 + 候选确认）
// ---------------------------------------------------------------------------

export type GuideInputType = 'text' | 'url' | 'image'
export type GuideParseStatus = 'pending' | 'parsed' | 'failed'

// 候选在用户侧的选择状态：必去 / 想去 / 可选 / 排除
export type UserChoice = 'required' | 'preferred' | 'optional' | 'excluded'

// 事实核验状态：已校验（地图命中）/ 未校验
export type FactStatus = 'verified' | 'partially_verified' | 'unverified'

// 候选地点：进入规划池前的统一中间形态（与后端 CandidatePlace 契约一致）
export interface CandidatePlace {
  candidate_id: string
  canonical_name: string
  aliases: string[]
  city: string
  poi_id?: string | null
  location?: Location | null
  category: string
  suggested_duration_minutes: number
  source_refs: string[]
  recommendation_reasons: string[]
  warnings: string[]
  confidence: number
  user_choice: UserChoice
  fact_status: FactStatus
}

// 攻略中的一条结构化事实
export interface GuideFact {
  fact_type: string
  subject: string
  claim: string
  confidence: number
}

// 单篇攻略解析结果
export interface GuideParseResult {
  source_id: string
  parse_status: GuideParseStatus
  error_message: string
  title: string
  city: string
  document_chars: number
  candidates: CandidatePlace[]
  facts: GuideFact[]
  error_hint?: string | null
}

// guide_sources 表记录（API 返回体）
export interface GuideSourceRecord {
  id: string
  input_type: GuideInputType
  source_url: string
  title: string
  content_hash: string
  parse_status: GuideParseStatus
  error_message: string
  created_at: string
  result?: GuideParseResult | null
}

// 提交攻略解析的请求体
export interface GuideSourcePayload {
  input_type: GuideInputType
  content?: string
  source_url?: string
  // input_type=image：截图 base64（不含 data: 前缀）与 MIME 类型
  image_data?: string
  image_mime?: string
  title?: string
  city?: string
}
