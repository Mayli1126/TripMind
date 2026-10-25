<template>
  <div class="result-container">
    <!-- 页面头部 -->
    <div class="page-header">
      <a-button class="back-button" size="large" @click="goBack">
        ← 返回首页
      </a-button>
      <a-space size="middle">
        <!-- tripPlan 为空（数据缺失/损坏）时禁用全部操作按钮，防止空态误操作崩溃 -->
        <a-button v-if="savedTrip" :disabled="!tripPlan" class="today-button" type="primary" @click="router.push(`/trips/${savedTrip.id}/today`)">
          ✓ 旅行计划表
        </a-button>
        <a-button v-if="savedTrip" :disabled="!tripPlan" @click="historyOpen = true">
          ↶ 历史版本 <a-badge :count="savedTrip.versions.length" :number-style="{ backgroundColor: '#ff6b4a' }" />
        </a-button>
        <a-button class="ai-revise-button" type="primary" :disabled="!tripPlan" @click="openRevisionDrawer">
          ✦ AI 修改行程
        </a-button>
        <a-button v-if="!editMode" :disabled="!tripPlan" @click="toggleEditMode" type="default">
          ✏️ 编辑行程
        </a-button>
        <a-button v-else @click="saveChanges" type="primary">
          💾 保存修改
        </a-button>
        <a-button v-if="editMode" @click="cancelEdit" type="default">
          ❌ 取消编辑
        </a-button>

        <!-- 导出按钮 -->
        <a-dropdown v-if="!editMode">
          <template #overlay>
            <a-menu>
              <a-menu-item key="image" @click="exportAsImage">
                📷 导出为图片
              </a-menu-item>
              <a-menu-item key="pdf" @click="exportAsPDF">
                📄 导出为PDF
              </a-menu-item>
            </a-menu>
          </template>
          <a-button type="default">
            📥 导出行程 <DownOutlined />
          </a-button>
        </a-dropdown>
      </a-space>
    </div>

    <a-alert
      v-if="tripPlan && tripPlan.warnings?.length"
      :type="tripPlan.is_fallback ? 'warning' : 'info'"
      :message="tripPlan.is_fallback ? '当前为降级参考计划' : '出行前核实提醒'"
      show-icon
      class="quality-alert"
    >
      <template #action>
        <a-button v-if="tripPlan.is_fallback" size="small" @click="goBack">调整条件并重试</a-button>
      </template>
      <template #description>
        <ul class="warning-list">
          <li v-for="warning in tripPlan.warnings || []" :key="warning">{{ warning }}</li>
        </ul>
      </template>
    </a-alert>

    <div v-if="tripPlan" class="content-wrapper">
      <!-- 侧边导航 -->
      <div class="side-nav">
        <a-affix :offset-top="80">
          <a-menu mode="inline" :selected-keys="[activeSection]" @click="scrollToSection">
            <a-menu-item key="overview">
              <span>📋 行程概览</span>
            </a-menu-item>
            <a-menu-item key="budget" v-if="tripPlan.budget">
              <span>💰 预算明细</span>
            </a-menu-item>
            <a-menu-item key="map">
              <span>📍 景点地图</span>
            </a-menu-item>
            <a-sub-menu key="days" title="📅 每日行程">
              <a-menu-item v-for="(day, index) in tripPlan.days" :key="`day-${index}`">
                第{{ day.day_index + 1 }}天
              </a-menu-item>
            </a-sub-menu>
            <a-menu-item key="weather" v-if="tripPlan.weather_info && tripPlan.weather_info.length > 0">
              <span>🌤️ 天气信息</span>
            </a-menu-item>
          </a-menu>
        </a-affix>
      </div>

      <!-- 主内容区 -->
      <div class="main-content">
        <!-- 顶部信息区:左侧概览+预算,右侧地图 -->
        <div class="top-info-section">
          <!-- 左侧:行程概览和预算明细 -->
          <div class="left-info">
            <!-- 行程概览 -->
            <a-card id="overview" :title="`${tripPlan.city}旅行计划`" :bordered="false" class="overview-card">
              <div class="overview-content">
                <div class="info-item">
                  <span class="info-label">📅 日期:</span>
                  <span class="info-value">{{ tripPlan.start_date }} 至 {{ tripPlan.end_date }}</span>
                </div>
                <div class="info-item">
                  <span class="info-label">💡 建议:</span>
                  <span class="info-value">{{ tripPlan.overall_suggestions }}</span>
                </div>
                <div class="info-item">
                  <span class="info-label">🔎 数据质量:</span>
                  <span class="info-value">{{ getQualityLabel(tripPlan.data_quality) }}</span>
                </div>
                <div class="info-item">
                  <span class="info-label">🤖 规划轮次:</span>
                  <span class="info-value">{{ tripPlan.planning_iterations }} 次</span>
                </div>
                <div v-if="tripPlan.optimization_summary" class="info-item">
                  <span class="info-label">🧭 路线优化:</span>
                  <span class="info-value">
                    {{ tripPlan.optimization_summary.total_distance_before_km }} km →
                    {{ tripPlan.optimization_summary.total_distance_after_km }} km，
                    节省 {{ tripPlan.optimization_summary.saved_percent }}%
                  </span>
                </div>
                <div v-if="tripPlan.data_sources.length" class="info-item">
                  <span class="info-label">📚 数据来源:</span>
                  <span class="info-value source-tags">
                    <a-tag
                      v-for="source in tripPlan.data_sources"
                      :key="`${source.category}-${source.name}`"
                      :color="getSourceColor(source.source_type)"
                      :title="source.note"
                    >
                      {{ source.name }} · {{ source.source_type }}
                    </a-tag>
                  </span>
                </div>
              </div>
            </a-card>

            <!-- 预算明细 -->
            <a-card id="budget" v-if="tripPlan.budget" title="💰 预算明细" :bordered="false" class="budget-card">
              <div class="budget-grid">
                <div class="budget-item">
                  <div class="budget-label">景点门票</div>
                  <div class="budget-value">¥{{ tripPlan.budget.total_attractions }}</div>
                </div>
                <div class="budget-item">
                  <div class="budget-label">酒店住宿</div>
                  <div class="budget-value">¥{{ tripPlan.budget.total_hotels }}</div>
                </div>
                <div class="budget-item">
                  <div class="budget-label">餐饮费用</div>
                  <div class="budget-value">¥{{ tripPlan.budget.total_meals }}</div>
                </div>
                <div class="budget-item">
                  <div class="budget-label">交通费用</div>
                  <div class="budget-value">¥{{ tripPlan.budget.total_transportation }}</div>
                </div>
                <div class="budget-item">
                  <div class="budget-label">人均费用</div>
                  <div class="budget-value">¥{{ tripPlan.budget.per_person }}</div>
                </div>
                <div class="budget-item">
                  <div class="budget-label">预算状态</div>
                  <div class="budget-value">{{ tripPlan.budget.over_budget ? '已超预算' : '预算内' }}</div>
                </div>
              </div>
              <div class="budget-total">
                <span class="total-label">预估总费用</span>
                <span class="total-value">¥{{ tripPlan.budget.total }}</span>
              </div>
              <p class="budget-note">{{ tripPlan.budget.calculation_note }}</p>
            </a-card>
          </div>

          <!-- 右侧:地图 -->
          <div class="right-map">
            <a-card id="map" title="📍 景点地图" :bordered="false" class="map-card">
              <div id="amap-container" style="width: 100%; height: 100%"></div>
            </a-card>
          </div>
        </div>

        <!-- 每日行程:可折叠 -->
        <a-card v-if="tripPlan.intercity_journey" class="intercity-card" :bordered="false">
          <div class="intercity-heading">
            <div>
              <span class="intercity-kicker">首日跨城交通</span>
              <h3>{{ tripPlan.intercity_journey.name }} · {{ tripPlan.intercity_journey.departure_time }}—{{ journeyArrivalLabel(tripPlan.intercity_journey) }}</h3>
              <p>{{ tripPlan.intercity_journey.departure_station }} → {{ tripPlan.intercity_journey.arrival_station }}</p>
            </div>
            <a-tag :color="tripPlan.intercity_journey.is_estimated ? 'orange' : 'green'">
              {{ tripPlan.intercity_journey.is_estimated ? '参考时刻，需复核' : '已按确认班次排程' }}
            </a-tag>
          </div>
          <div class="intercity-buffers">
            <span>提前 {{ tripPlan.intercity_journey.station_transfer_minutes }} 分钟前往车站 / 机场</span>
            <span>预留 {{ tripPlan.intercity_journey.check_in_buffer_minutes }} 分钟进站 / 值机</span>
            <span>抵达后预留 {{ tripPlan.intercity_journey.arrival_transfer_minutes }} 分钟接驳</span>
            <span>首个景点从接驳完成后开始</span>
          </div>
        </a-card>

        <a-card v-if="tripPlan.return_journey" class="intercity-card return-card" :bordered="false">
          <div class="intercity-heading">
            <div>
              <span class="intercity-kicker">末日返程交通</span>
              <h3>{{ tripPlan.return_journey.name }} · {{ tripPlan.return_journey.departure_time }}—{{ journeyArrivalLabel(tripPlan.return_journey) }}</h3>
              <p>{{ tripPlan.return_journey.departure_station }} → {{ tripPlan.return_journey.arrival_station }}</p>
            </div>
            <a-tag color="green">已锁定返程时间</a-tag>
          </div>
          <div class="intercity-buffers">
            <span>提前 {{ tripPlan.return_journey.station_transfer_minutes }} 分钟前往车站 / 机场</span>
            <span>预留 {{ tripPlan.return_journey.check_in_buffer_minutes }} 分钟安检候车</span>
            <span>末日活动会在出发前结束</span>
          </div>
        </a-card>

        <a-card :bordered="false" class="days-card">
          <template #title>
            <div class="days-card-title">
              <span>📅 每日行程</span>
              <a-button size="small" @click.stop="toggleAllDays">
                {{ allDaysExpanded ? '全部收起' : '全部展开' }}
              </a-button>
            </div>
          </template>
          <a-collapse v-model:activeKey="activeDays">
            <a-collapse-panel
              v-for="(day, index) in tripPlan.days"
              :key="index"
              :id="`day-${index}`"
            >
              <template #header>
                <div class="day-header">
                  <div><span class="day-title">第{{ day.day_index + 1 }}天</span><b v-if="day.theme">{{ day.theme }}</b></div>
                  <span class="day-date">{{ day.date }} · {{ day.schedule?.length ? scheduleTimeLabel(day.schedule[0].start_time, day.schedule[0].start_day_offset) : '08:30' }}—{{ day.schedule?.length ? scheduleTimeLabel(day.schedule[day.schedule.length - 1].end_time, day.schedule[day.schedule.length - 1].end_day_offset) : '20:00' }}</span>
                </div>
              </template>

              <!-- 行程基本信息 -->
              <div class="day-info">
                <div class="info-row">
                  <span class="label">📝 行程描述:</span>
                  <span class="value">{{ day.description }}</span>
                </div>
                <div class="info-row">
                  <span class="label">🚗 交通方式:</span>
                  <span class="value">{{ day.transportation }}</span>
                </div>
                <div class="info-row">
                  <span class="label">🏨 住宿:</span>
                  <span class="value">{{ day.accommodation }}</span>
                </div>
                <div v-if="day.route_summary" class="info-row">
                  <span class="label">🧭 路线优化:</span>
                  <span class="value">
                    约 {{ day.route_summary.distance_after_km }} km / {{ day.route_summary.estimated_travel_minutes }} 分钟
                    <a-tag v-if="day.route_summary.saved_percent > 0" color="green">
                      减少 {{ day.route_summary.saved_percent }}%
                    </a-tag>
                    <small v-if="day.route_summary.start_anchor" class="route-anchor-note">
                      从 {{ day.route_summary.start_anchor }} 出发，首段约 {{ day.route_summary.start_transfer_km }} km
                      <template v-if="day.route_summary.end_anchor">
                        · 最后一站至 {{ day.route_summary.end_anchor }} 约 {{ day.route_summary.end_transfer_km }} km
                      </template>
                    </small>
                  </span>
                </div>
              </div>

              <a-divider v-if="day.schedule?.length" orientation="left">⏱️ 精细时间安排</a-divider>
              <div v-if="day.schedule?.length" class="daily-timeline">
                <div v-for="(slot, slotIndex) in day.schedule" :key="`${slot.start_time}-${slotIndex}`" class="timeline-slot" :class="`slot-${slot.item_type}`">
                  <div class="slot-time"><b>{{ scheduleTimeLabel(slot.start_time, slot.start_day_offset) }}</b><span>{{ scheduleTimeLabel(slot.end_time, slot.end_day_offset) }}</span></div>
                  <div class="slot-marker">{{ getScheduleIcon(slot.item_type) }}</div>
                  <div class="slot-content">
                    <div><h4>{{ slot.title }}</h4><a-tag>{{ slot.duration_minutes }} 分钟</a-tag></div>
                    <p v-if="slot.description">{{ slot.description }}</p>
                    <small v-if="slot.address">⌖ {{ slot.address }}</small>
                    <em v-if="slot.notes">{{ slot.notes }}</em>
                  </div>
                </div>
              </div>

              <!-- 景点安排 -->
              <a-divider orientation="left">🎯 景点安排</a-divider>
              <a-list
                :data-source="day.attractions"
                :grid="{ gutter: 16, column: 2 }"
              >
                <template #renderItem="{ item, index }">
                  <a-list-item>
                    <a-card :title="item.name" size="small" class="attraction-card">
                      <!-- 编辑模式下的操作按钮 -->
                      <template #extra v-if="editMode">
                        <a-space>
                          <a-button
                            size="small"
                            @click="moveAttraction(day.day_index, index, 'up')"
                            :disabled="index === 0"
                          >
                            ↑
                          </a-button>
                          <a-button
                            size="small"
                            @click="moveAttraction(day.day_index, index, 'down')"
                            :disabled="index === day.attractions.length - 1"
                          >
                            ↓
                          </a-button>
                          <a-button
                            size="small"
                            danger
                            @click="deleteAttraction(day.day_index, index)"
                          >
                            🗑️
                          </a-button>
                        </a-space>
                      </template>

                      <!-- 景点图片 -->
                      <div class="attraction-image-wrapper" :data-attraction-name="item.name">
                        <img
                          :src="getAttractionImage(item.name, index)"
                          :alt="item.name"
                          class="attraction-image"
                          @error="handleImageError"
                        />
                        <div class="attraction-badge">
                          <span class="badge-number">{{ index + 1 }}</span>
                        </div>
                        <div v-if="item.ticket_price" class="price-tag">
                          ¥{{ item.ticket_price }}
                        </div>
                        <a
                          v-if="attractionPhotoMeta[item.name]?.source"
                          class="photo-source"
                          :href="attractionPhotoMeta[item.name].sourceUrl || undefined"
                          target="_blank"
                          rel="noreferrer"
                          :title="attractionPhotoMeta[item.name].attribution"
                        >
                          图片 · {{ attractionPhotoMeta[item.name].source }}
                        </a>
                      </div>

                      <!-- 编辑模式下可编辑的字段 -->
                      <div v-if="editMode">
                        <p><strong>地址:</strong></p>
                        <a-input v-model:value="item.address" size="small" style="margin-bottom: 8px" />

                        <p><strong>游览时长(分钟):</strong></p>
                        <a-input-number v-model:value="item.visit_duration" :min="10" :max="480" size="small" style="width: 100%; margin-bottom: 8px" />

                        <p><strong>描述:</strong></p>
                        <a-textarea v-model:value="item.description" :rows="2" size="small" style="margin-bottom: 8px" />
                      </div>

                      <!-- 查看模式 -->
                      <div v-else>
                        <p><strong>地址:</strong> {{ item.address }}</p>
                        <p><strong>游览时长:</strong> {{ item.visit_duration }}分钟</p>
                        <p><strong>描述:</strong> {{ item.description }}</p>
                        <p v-if="item.rating"><strong>评分:</strong> {{ item.rating }}⭐</p>
                      </div>
                    </a-card>
                  </a-list-item>
                </template>
              </a-list>

              <!-- 酒店推荐 -->
              <a-divider v-if="day.hotel" orientation="left">🏨 住宿推荐</a-divider>
              <a-card v-if="day.hotel" size="small" class="hotel-card">
                <template #title>
                  <span class="hotel-title">{{ day.hotel.name }}</span>
                </template>
                <a-descriptions :column="2" size="small">
                  <a-descriptions-item label="地址">{{ day.hotel.address }}</a-descriptions-item>
                  <a-descriptions-item label="类型">{{ day.hotel.type }}</a-descriptions-item>
                  <a-descriptions-item label="价格范围">{{ day.hotel.price_range }}</a-descriptions-item>
                  <a-descriptions-item label="评分">{{ day.hotel.rating }}⭐</a-descriptions-item>
                  <a-descriptions-item label="距离" :span="2">{{ day.hotel.distance }}</a-descriptions-item>
                </a-descriptions>
              </a-card>

              <!-- 餐饮安排 -->
              <a-divider orientation="left">🍽️ 餐饮安排</a-divider>
              <a-descriptions :column="1" bordered size="small">
                <a-descriptions-item
                  v-for="meal in day.meals"
                  :key="meal.type"
                  :label="getMealLabel(meal.type)"
                >
                  {{ meal.name }}
                  <span v-if="meal.description"> - {{ meal.description }}</span>
                </a-descriptions-item>
              </a-descriptions>
            </a-collapse-panel>
          </a-collapse>
        </a-card>

        <a-card id="weather" v-if="tripPlan.weather_info && tripPlan.weather_info.length > 0" title="天气信息" style="margin-top: 20px" :bordered="false">
        <a-list
          :data-source="tripPlan.weather_info"
          :grid="{ gutter: 16, column: 3 }"
        >
          <template #renderItem="{ item }">
            <a-list-item>
              <a-card size="small" class="weather-card">
                <div class="weather-date">{{ item.date }}</div>
                <div class="weather-info-row">
                  <span class="weather-icon">☀️</span>
                  <div>
                    <div class="weather-label">白天</div>
                    <div class="weather-value">{{ item.day_weather }} {{ item.day_temp }}°C</div>
                  </div>
                </div>
                <div class="weather-info-row">
                  <span class="weather-icon">🌙</span>
                  <div>
                    <div class="weather-label">夜间</div>
                    <div class="weather-value">{{ item.night_weather }} {{ item.night_temp }}°C</div>
                  </div>
                </div>
                <div class="weather-wind">
                  💨 {{ item.wind_direction }} {{ item.wind_power }}
                </div>
              </a-card>
            </a-list-item>
          </template>
        </a-list>
        </a-card>
      </div>
    </div>

    <a-empty v-else description="没有找到旅行计划数据">
      <template #image>
        <div style="font-size: 80px;">🗺️</div>
      </template>
      <template #description>
        <span style="color: #999;">暂无旅行计划数据,请先创建行程</span>
      </template>
      <a-button type="primary" @click="goBack">返回首页创建行程</a-button>
    </a-empty>

    <a-drawer
      v-model:open="revisionOpen"
      title="AI 修改行程"
      placement="right"
      width="min(460px, 100vw)"
      :closable="!revisionLoading"
      class="revision-drawer"
    >
      <div class="revision-intro">
        <span class="revision-agent-mark">✦</span>
        <div>
          <strong>告诉 TripMind 你想调整什么</strong>
          <p>系统只修改相关部分，随后重新校验日期、三餐、预算和路线。</p>
        </div>
      </div>

      <div class="quick-prompts">
        <span>快捷指令</span>
        <button v-for="prompt in quickRevisionPrompts" :key="prompt" @click="revisionInstruction = prompt">
          {{ prompt }}
        </button>
      </div>

      <a-textarea
        v-model:value="revisionInstruction"
        :rows="5"
        :maxlength="1000"
        show-count
        :disabled="revisionLoading"
        placeholder="例如：第二天安排轻松一点，减少一个景点，并把晚餐换成当地小吃。"
      />

      <div v-if="revisionLoading" class="revision-progress">
        <div class="revision-progress-head">
          <span>{{ revisionStatus }}</span><b>{{ revisionProgress }}%</b>
        </div>
        <a-progress :percent="revisionProgress" :show-info="false" />
        <div class="revision-events">
          <span v-for="event in revisionEvents" :key="event.sequence">
            <i>{{ getRevisionStageIcon(event.stage) }}</i>{{ event.message }}
          </span>
        </div>
      </div>

      <div v-if="revisionHistory.length" class="revision-history">
        <span>本次会话已修改</span>
        <p v-for="item in revisionHistory" :key="item">✓ {{ item }}</p>
      </div>

      <template #footer>
        <div class="revision-footer">
          <span>修改失败时会自动保留当前计划</span>
          <a-button
            type="primary"
            :loading="revisionLoading"
            :disabled="revisionInstruction.trim().length < 2"
            @click="submitRevision"
          >
            应用修改
          </a-button>
        </div>
      </template>
    </a-drawer>

    <!-- 修改差异确认：确认前不改动当前行程 -->
    <a-modal
      v-model:open="diffOpen"
      title="确认 AI 修改内容"
      :width="480"
      :mask-closable="false"
      ok-text="应用修改"
      cancel-text="放弃本次修改"
      @ok="confirmRevision"
      @cancel="discardRevision"
    >
      <revision-diff
        v-if="pendingRevision"
        :changes="pendingRevision.plan.revision_diff || []"
        :explanation="pendingRevision.explanation"
      />
    </a-modal>

    <a-drawer
      v-model:open="historyOpen"
      title="历史版本"
      placement="right"
      width="min(420px, 100vw)"
    >
      <div class="history-intro">
        <strong>每次修改都可以反悔</strong>
        <p>恢复旧版本不会覆盖历史，而是创建一个新的版本。</p>
      </div>
      <a-timeline v-if="savedTrip">
        <a-timeline-item v-for="version in savedTrip.versions" :key="version.version" :color="version.version === savedTrip.current_version ? 'green' : 'gray'">
          <div class="version-item">
            <div><b>v{{ version.version }}</b><span>{{ version.change_note }}</span></div>
            <small>{{ formatVersionDate(version.created_at) }}</small>
            <a-button v-if="version.version !== savedTrip.current_version" size="small" :loading="restoringVersion === version.version" @click="restoreVersion(version.version)">恢复此版本</a-button>
            <a-tag v-else color="green">当前版本</a-tag>
          </div>
        </a-timeline-item>
      </a-timeline>
    </a-drawer>

    <!-- 回到顶部按钮 -->
    <a-back-top :visibility-height="300">
      <div class="back-top-button">
        ↑
      </div>
    </a-back-top>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Affix as AAffix,
  Alert as AAlert,
  BackTop as ABackTop,
  Badge as ABadge,
  Button as AButton,
  Card as ACard,
  Collapse as ACollapse,
  CollapsePanel as ACollapsePanel,
  Descriptions as ADescriptions,
  DescriptionsItem as ADescriptionsItem,
  Divider as ADivider,
  Drawer as ADrawer,
  Dropdown as ADropdown,
  Empty as AEmpty,
  Input as AInput,
  InputNumber as AInputNumber,
  List as AList,
  ListItem as AListItem,
  Menu as AMenu,
  MenuItem as AMenuItem,
  Progress as AProgress,
  Space as ASpace,
  SubMenu as ASubMenu,
  Tag as ATag,
  Textarea as ATextarea,
  Timeline as ATimeline,
  TimelineItem as ATimelineItem,
  message
} from 'ant-design-vue'
import { DownOutlined } from '@ant-design/icons-vue'
import type { MenuProps } from 'ant-design-vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import { getSavedTrip, restoreTripVersion, reviseTripWithProgress, saveTripVersion } from '@/services/api'
import type { PlanningProgressEvent, RevisionChange, SavedTripDetail, TripFormData, TripPlan } from '@/types'
import RevisionDiff from '@/components/RevisionDiff.vue'

const router = useRouter()
const route = useRoute()
const tripPlan = ref<TripPlan | null>(null)
const editMode = ref(false)
const originalPlan = ref<TripPlan | null>(null)
const attractionPhotos = ref<Record<string, string>>({})
const attractionPhotoMeta = ref<Record<string, { source: string; sourceUrl?: string; attribution?: string }>>({})
const activeSection = ref('overview')
const activeDays = ref<number[]>([0]) // 默认展开第一天
const allDaysExpanded = computed(() => Boolean(
  tripPlan.value?.days.length && activeDays.value.length === tripPlan.value.days.length
))
const tripRequest = ref<TripFormData | null>(null)
const revisionOpen = ref(false)
const revisionInstruction = ref('')
const revisionLoading = ref(false)
const revisionProgress = ref(0)
const revisionStatus = ref('')
const revisionEvents = ref<PlanningProgressEvent[]>([])
const revisionHistory = ref<string[]>([])
const pendingRevision = ref<{ plan: TripPlan; instruction: string; explanation: string } | null>(null)
const diffOpen = ref(false)
const savedTrip = ref<SavedTripDetail | null>(null)
const historyOpen = ref(false)
const restoringVersion = ref<number | null>(null)
const quickRevisionPrompts = [
  '第二天安排轻松一点',
  '减少重复景点并优化路线',
  '优先保留免费景点',
  '把餐饮换成当地特色小吃'
]
let map: any = null

onMounted(async () => {
  const savedTripId = typeof route.params.id === 'string' ? route.params.id : ''
  if (savedTripId) {
    try {
      savedTrip.value = await getSavedTrip(savedTripId)
      tripPlan.value = savedTrip.value.plan
      tripRequest.value = savedTrip.value.request
      await refreshPlanVisuals()
      return
    } catch (error: any) {
      message.error(error.response?.status === 404 ? '这份行程不存在或已被删除' : '暂时无法读取行程')
      router.push('/trips')
      return
    }
  }
  const data = sessionStorage.getItem('tripPlan')
  const requestData = sessionStorage.getItem('tripRequest')
  if (requestData) {
    try { tripRequest.value = JSON.parse(requestData) } catch { tripRequest.value = null }
  }
  if (data) {
    try {
      tripPlan.value = JSON.parse(data)
    } catch {
      sessionStorage.removeItem('tripPlan')
      message.error('行程数据已损坏，请重新生成')
      router.push('/')
      return
    }
    await refreshPlanVisuals()
  }
})

const refreshPlanVisuals = async () => {
  await nextTick()
  // 地图优先加载，不再等待图片
  if (map) map.destroy()
  initMap()
  // 图片异步加载，不阻塞地图
  loadAttractionPhotos()
}

// 路由离开时销毁地图实例，防止高德 JSAPI 实例随反复进出页面而累积泄漏
onUnmounted(() => {
  if (map) {
    try {
      map.destroy()
    } catch {
      // 地图 SDK 已自行回收时忽略
    }
    map = null
  }
})

const goBack = () => {
  router.push('/')
}

const openRevisionDrawer = () => {
  if (!tripRequest.value) {
    message.warning('当前行程缺少原始约束，请从首页重新生成后再使用 AI 修改')
    return
  }
  revisionOpen.value = true
}

const submitRevision = async () => {
  if (!tripPlan.value || !tripRequest.value || revisionInstruction.value.trim().length < 2) return
  const instruction = revisionInstruction.value.trim()
  revisionLoading.value = true
  revisionProgress.value = 0
  revisionStatus.value = '正在创建修改任务...'
  revisionEvents.value = []
  try {
    const revisedPlan = await reviseTripWithProgress({
      trip_request: tripRequest.value,
      plan: tripPlan.value,
      instruction
    }, (event) => {
      revisionProgress.value = event.progress
      revisionStatus.value = event.message
      const previous = revisionEvents.value[revisionEvents.value.length - 1]
      if (!previous || previous.stage !== event.stage || previous.message !== event.message) {
        revisionEvents.value.push(event)
      }
    })
    // 阶段1批1：先展示差异确认，用户确认后才应用
    const diff = (revisedPlan.revision_diff || []) as RevisionChange[]
    pendingRevision.value = {
      plan: revisedPlan,
      instruction,
      explanation: diff.length ? `AI 理解的修改：${instruction}` : `修改范围较大：${instruction}`,
    }
    revisionOpen.value = false
    diffOpen.value = true
  } catch (error: any) {
    message.error(error.message || '修改失败，当前计划未发生变化')
  } finally {
    revisionLoading.value = false
  }
}

const confirmRevision = async () => {
  const pending = pendingRevision.value
  if (!pending) return
  const revisedPlan = pending.plan
  try {
    // 先落版本再切本地状态：保存失败时保持差异弹窗打开，用户可重试，不产生本地/服务端不一致
    if (savedTrip.value) {
      savedTrip.value = await saveTripVersion(savedTrip.value.id, revisedPlan, `AI 修改：${pending.instruction}`)
      tripPlan.value = savedTrip.value.plan
    } else {
      tripPlan.value = revisedPlan
      sessionStorage.setItem('tripPlan', JSON.stringify(revisedPlan))
    }
  } catch (error: any) {
    message.error(error.message || '保存修改版本失败，请重试或放弃本次修改')
    return
  }
  revisionHistory.value.unshift(pending.instruction)
  revisionInstruction.value = ''
  diffOpen.value = false
  pendingRevision.value = null
  message.success('行程已修改，并重新完成预算与路线校验')
  await refreshPlanVisuals()
}

const discardRevision = () => {
  // 放弃：保留当前计划，不落任何版本
  pendingRevision.value = null
  diffOpen.value = false
  revisionInstruction.value = ''
  message.info('已放弃本次修改，当前计划保持不变')
}

const getRevisionStageIcon = (stage: string): string => ({
  queued: '◷', starting: '✦', understanding: '◎', revising: '✎',
  validating: '✓', optimizing: '◇', completed: '●'
}[stage] || '·')

const toggleAllDays = () => {
  activeDays.value = allDaysExpanded.value
    ? []
    : (tripPlan.value?.days.map((_, index) => index) || [])
}

const getScheduleIcon = (type: string) => ({
  meal: '餐', transport: '行', attraction: '游', hotel: '住', break: '歇'
}[type] || '·')

const scheduleTimeLabel = (time: string, dayOffset = 0) => `${dayOffset === 1 ? '次日 ' : dayOffset > 1 ? `次日+${dayOffset - 1} ` : ''}${time}`
const journeyArrivalLabel = (journey: NonNullable<TripPlan['intercity_journey']>) => {
  const dayOffset = Math.round(
    (new Date(`${journey.arrival_date}T00:00:00`).getTime() - new Date(`${journey.departure_date}T00:00:00`).getTime()) / 86400000
  )
  return scheduleTimeLabel(journey.arrival_time, dayOffset)
}

// 滚动到指定区域
const scrollToSection = ({ key }: Parameters<NonNullable<MenuProps['onClick']>>[0]) => {
  const sectionKey = String(key)
  activeSection.value = sectionKey
  const element = document.getElementById(sectionKey)
  if (element) {
    element.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

// 切换编辑模式
const toggleEditMode = () => {
  editMode.value = true
  // 保存原始数据用于取消编辑
  originalPlan.value = JSON.parse(JSON.stringify(tripPlan.value))
  message.info('进入编辑模式')
}

// 保存修改
const saveChanges = async () => {
  editMode.value = false
  try {
    if (tripPlan.value) {
      sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
      if (savedTrip.value) {
        savedTrip.value = await saveTripVersion(savedTrip.value.id, tripPlan.value, '手动调整景点顺序或内容')
        tripPlan.value = savedTrip.value.plan
      }
    }
    message.success('修改已保存为新版本')
  } catch (error: any) {
    message.error(error.message || '修改暂未保存，请重试')
  }

  // 重新初始化地图以反映更改
  if (map) {
    map.destroy()
  }
  nextTick(() => {
    initMap()
  })
}

const formatVersionDate = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
}).format(new Date(value))

const restoreVersion = async (version: number) => {
  if (!savedTrip.value) return
  restoringVersion.value = version
  try {
    savedTrip.value = await restoreTripVersion(savedTrip.value.id, version)
    tripPlan.value = savedTrip.value.plan
    sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
    historyOpen.value = false
    await refreshPlanVisuals()
    message.success(`已恢复 v${version}，并保存为新版本`)
  } catch (error: any) {
    message.error(error.message || '恢复失败')
  } finally {
    restoringVersion.value = null
  }
}

// 取消编辑
const cancelEdit = () => {
  if (originalPlan.value) {
    tripPlan.value = JSON.parse(JSON.stringify(originalPlan.value))
  }
  editMode.value = false
  message.info('已取消编辑')
}

// 删除景点
const deleteAttraction = (dayIndex: number, attrIndex: number) => {
  if (!tripPlan.value) return

  const day = tripPlan.value.days[dayIndex]
  if (day.attractions.length <= 1) {
    message.warning('每天至少需要保留一个景点')
    return
  }

  day.attractions.splice(attrIndex, 1)
  message.success('景点已删除')
}

// 移动景点顺序
const moveAttraction = (dayIndex: number, attrIndex: number, direction: 'up' | 'down') => {
  if (!tripPlan.value) return

  const day = tripPlan.value.days[dayIndex]
  const attractions = day.attractions

  if (direction === 'up' && attrIndex > 0) {
    [attractions[attrIndex], attractions[attrIndex - 1]] = [attractions[attrIndex - 1], attractions[attrIndex]]
  } else if (direction === 'down' && attrIndex < attractions.length - 1) {
    [attractions[attrIndex], attractions[attrIndex + 1]] = [attractions[attrIndex + 1], attractions[attrIndex]]
  }
}

const getMealLabel = (type: string): string => {
  const labels: Record<string, string> = {
    breakfast: '早餐',
    lunch: '午餐',
    dinner: '晚餐',
    snack: '小吃'
  }
  return labels[type] || type
}

const getQualityLabel = (quality: TripPlan['data_quality']): string => ({
  live: '实时数据',
  mixed: '实时检索与估算混合',
  fallback: '静态参考模板'
}[quality])

const getSourceColor = (sourceType: string): string => ({
  live: 'green',
  estimated: 'orange',
  generated: 'blue',
  static: 'red'
}[sourceType] || 'default')

const escapeHtml = (value: unknown): string => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#039;')

// 图片缓存key前缀
const IMAGE_CACHE_PREFIX = 'tripmind_image_'

// 从localStorage获取图片缓存
const getImageCache = (key: string): { photoUrl: string; meta: { source: string; sourceUrl?: string; attribution?: string }; timestamp: number } | null => {
  try {
    const cached = localStorage.getItem(IMAGE_CACHE_PREFIX + key)
    if (!cached) return null
    const parsed = JSON.parse(cached)
    // 缓存有效期7天
    if (parsed.timestamp && Date.now() - parsed.timestamp < 7 * 24 * 60 * 60 * 1000) {
      return parsed
    }
    // 过期缓存清理
    localStorage.removeItem(IMAGE_CACHE_PREFIX + key)
    return null
  } catch {
    return null
  }
}

// 保存图片到localStorage缓存
const setImageCache = (key: string, photoUrl: string, meta: { source: string; sourceUrl?: string; attribution?: string }) => {
  try {
    localStorage.setItem(IMAGE_CACHE_PREFIX + key, JSON.stringify({
      photoUrl,
      meta,
      timestamp: Date.now()
    }))
  } catch {
    // localStorage满或不可用，忽略
  }
}

// 需要加载图片的景点队列
const pendingImages = ref<{ name: string; city: string }[]>([])
// 已加载的图片集合（用于去重）
const loadedImages = ref<Set<string>>(new Set())
let imageObserver: IntersectionObserver | null = null

// 获取图片URL（不带缓存查询，直接发起请求）
const fetchAttractionImage = async (name: string, city: string) => {
  const cacheKey = `${name}_${city}`
  
  // 检查本地缓存
  const cached = getImageCache(cacheKey)
  if (cached) {
    attractionPhotos.value[name] = cached.photoUrl
    attractionPhotoMeta.value[name] = cached.meta
    return
  }

  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
  try {
    const res = await fetch(`${apiBaseUrl}/api/poi/photo?name=${encodeURIComponent(name)}&city=${encodeURIComponent(city)}`)
    const data = await res.json()
    if (data.success && data.data.photo_url) {
      const meta = {
        source: data.data.source,
        sourceUrl: data.data.source_url,
        attribution: data.data.attribution
      }
      attractionPhotos.value[name] = data.data.photo_url
      attractionPhotoMeta.value[name] = meta
      // 保存到缓存
      setImageCache(cacheKey, data.data.photo_url, meta)
    }
  } catch (err) {
    console.error(`获取${name}图片失败:`, err)
  }
}

// 加载指定景点的图片
const loadSingleAttractionImage = (name: string, city: string) => {
  if (loadedImages.value.has(name)) return
  loadedImages.value.add(name)
  fetchAttractionImage(name, city)
}

// 初始化图片懒加载
const initImageLazyLoad = () => {
  if (!tripPlan.value) return

  const city = tripPlan.value.city || ''
  pendingImages.value = []

  // 收集所有需要加载的景点
  tripPlan.value.days.forEach(day => {
    day.attractions.forEach(attraction => {
      const poiImage = attraction.image_url || attraction.photos?.[0]
      if (poiImage) {
        attractionPhotos.value[attraction.name] = poiImage
        attractionPhotoMeta.value[attraction.name] = { source: '高德 POI' }
        loadedImages.value.add(attraction.name)
        return
      }

      // 检查本地缓存
      const cacheKey = `${attraction.name}_${city}`
      const cached = getImageCache(cacheKey)
      if (cached) {
        attractionPhotos.value[attraction.name] = cached.photoUrl
        attractionPhotoMeta.value[attraction.name] = cached.meta
        loadedImages.value.add(attraction.name)
        return
      }

      pendingImages.value.push({ name: attraction.name, city })
    })
  })

  // 使用IntersectionObserver实现懒加载
  if (imageObserver) {
    imageObserver.disconnect()
  }

  imageObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const name = entry.target.getAttribute('data-attraction-name')
          if (name && !loadedImages.value.has(name)) {
            loadSingleAttractionImage(name, city)
          }
          // 不再观察已加载的元素
          imageObserver?.unobserve(entry.target)
        }
      })
    },
    {
      rootMargin: '100px', // 提前100px开始加载
      threshold: 0.1
    }
  )

  // 观察所有景点图片容器
  nextTick(() => {
    document.querySelectorAll('[data-attraction-name]').forEach(el => {
      imageObserver?.observe(el)
    })
  })
}

// 加载所有景点图片（兼容旧逻辑，实际使用懒加载）
const loadAttractionPhotos = async () => {
  initImageLazyLoad()
}

// 获取景点图片
const getAttractionImage = (name: string, index: number): string => {
  // 如果已加载真实图片,返回真实图片
  if (attractionPhotos.value[name]) {
    return attractionPhotos.value[name]
  }

  // 返回一个纯色占位图(避免跨域问题)
  const colors = [
    { start: '#667eea', end: '#764ba2' },
    { start: '#f093fb', end: '#f5576c' },
    { start: '#4facfe', end: '#00f2fe' },
    { start: '#43e97b', end: '#38f9d7' },
    { start: '#fa709a', end: '#fee140' }
  ]
  const colorIndex = index % colors.length
  const { start, end } = colors[colorIndex]

  // 使用base64编码避免中文问题
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
    <defs>
      <linearGradient id="grad${index}" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" style="stop-color:${start};stop-opacity:1" />
        <stop offset="100%" style="stop-color:${end};stop-opacity:1" />
      </linearGradient>
    </defs>
    <rect width="400" height="300" fill="url(#grad${index})"/>
    <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="24" font-weight="bold" fill="white">${name}</text>
  </svg>`

  return `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(svg)))}`
}

// 图片加载失败时的处理
const handleImageError = (event: Event) => {
  const img = event.target as HTMLImageElement
  // 使用灰色占位图
  img.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="400" height="300"%3E%3Crect width="400" height="300" fill="%23f0f0f0"/%3E%3Ctext x="50%25" y="50%25" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="18" fill="%23999"%3E图片加载失败%3C/text%3E%3C/svg%3E'
}



// 导出为图片
const exportAsImage = async () => {
  try {
    message.loading({ content: '正在生成图片...', key: 'export', duration: 0 })
    const { default: html2canvas } = await import('html2canvas')

    const element = document.querySelector('.main-content') as HTMLElement
    if (!element) {
      throw new Error('未找到内容元素')
    }

    // 创建一个独立的容器
    const exportContainer = document.createElement('div')
    exportContainer.style.width = element.offsetWidth + 'px'
    exportContainer.style.backgroundColor = '#f5f7fa'
    exportContainer.style.padding = '20px'

    // 复制所有内容
    exportContainer.innerHTML = element.innerHTML

    // 处理地图截图
    const mapContainer = document.getElementById('amap-container')
    if (mapContainer && map) {
      const mapCanvas = mapContainer.querySelector('canvas')
      if (mapCanvas) {
        const mapSnapshot = mapCanvas.toDataURL('image/png')
        const exportMapContainer = exportContainer.querySelector('#amap-container')
        if (exportMapContainer) {
          exportMapContainer.innerHTML = `<img src="${mapSnapshot}" style="width:100%;height:100%;object-fit:cover;" />`
        }
      }
    }

    // 移除所有ant-card类,替换为纯div
    const cards = exportContainer.querySelectorAll('.ant-card')
    cards.forEach((card) => {
      const cardEl = card as HTMLElement
      try {
        cardEl.className = '' // 移除所有类
        cardEl.style.setProperty('background-color', '#ffffff')
        cardEl.style.setProperty('border-radius', '12px')
        cardEl.style.setProperty('box-shadow', '0 4px 12px rgba(0, 0, 0, 0.1)')
        cardEl.style.setProperty('margin-bottom', '20px')
        cardEl.style.setProperty('overflow', 'hidden')
      } catch (err) {
        console.error('设置卡片样式失败:', err)
      }
    })

    // 处理卡片头部
    const cardHeads = exportContainer.querySelectorAll('.ant-card-head')
    cardHeads.forEach((head) => {
      const headEl = head as HTMLElement
      try {
        headEl.style.setProperty('background-color', '#667eea')
        headEl.style.setProperty('color', '#ffffff')
        headEl.style.setProperty('padding', '16px 24px')
        headEl.style.setProperty('font-size', '18px')
        headEl.style.setProperty('font-weight', '600')
      } catch (err) {
        console.error('设置卡片头部样式失败:', err)
      }
    })

    // 处理卡片内容
    const cardBodies = exportContainer.querySelectorAll('.ant-card-body')
    cardBodies.forEach((body) => {
      const bodyEl = body as HTMLElement
      bodyEl.style.setProperty('background-color', '#ffffff')
      bodyEl.style.setProperty('padding', '24px')
    })

    // 处理酒店卡片头部
    const hotelCards = exportContainer.querySelectorAll('.hotel-card')
    hotelCards.forEach((card) => {
      const head = card.querySelector('.ant-card-head') as HTMLElement
      if (head) {
        head.style.setProperty('background-color', '#1976d2')
      }
      (card as HTMLElement).style.setProperty('background-color', '#e3f2fd')
    })

    // 处理天气卡片
    const weatherCards = exportContainer.querySelectorAll('.weather-card')
    weatherCards.forEach((card) => {
      (card as HTMLElement).style.setProperty('background-color', '#e0f7fa')
    })

    // 处理预算总计
    const budgetTotal = exportContainer.querySelector('.budget-total')
    if (budgetTotal) {
      const el = budgetTotal as HTMLElement
      el.style.setProperty('background-color', '#667eea')
      el.style.setProperty('color', '#ffffff')
      el.style.setProperty('padding', '20px')
      el.style.setProperty('border-radius', '12px')
      el.style.setProperty('margin-bottom', '20px')
    }

    // 处理预算项
    const budgetItems = exportContainer.querySelectorAll('.budget-item')
    budgetItems.forEach((item) => {
      const el = item as HTMLElement
      el.style.setProperty('background-color', '#f5f7fa')
      el.style.setProperty('padding', '16px')
      el.style.setProperty('border-radius', '8px')
      el.style.setProperty('margin-bottom', '12px')
    })

    // 添加到body(隐藏)
    exportContainer.style.position = 'absolute'
    exportContainer.style.left = '-9999px'
    document.body.appendChild(exportContainer)

    const canvas = await html2canvas(exportContainer, {
      backgroundColor: '#f5f7fa',
      scale: 2,
      logging: false,
      useCORS: true,
      allowTaint: true
    })

    // 移除容器
    document.body.removeChild(exportContainer)

    // 转换为图片并下载
    const link = document.createElement('a')
    link.download = `旅行计划_${tripPlan.value?.city}_${new Date().getTime()}.png`
    link.href = canvas.toDataURL('image/png')
    link.click()

    message.success({ content: '图片导出成功!', key: 'export' })
  } catch (error: any) {
    console.error('导出图片失败:', error)
    message.error({ content: `导出图片失败: ${error.message}`, key: 'export' })
  }
}

// 导出为PDF
const exportAsPDF = async () => {
  try {
    message.loading({ content: '正在生成PDF...', key: 'export', duration: 0 })
    const [{ default: html2canvas }, { default: jsPDF }] = await Promise.all([
      import('html2canvas'),
      import('jspdf')
    ])

    const element = document.querySelector('.main-content') as HTMLElement
    if (!element) {
      throw new Error('未找到内容元素')
    }

    // 创建一个独立的容器
    const exportContainer = document.createElement('div')
    exportContainer.style.width = element.offsetWidth + 'px'
    exportContainer.style.backgroundColor = '#f5f7fa'
    exportContainer.style.padding = '20px'

    // 复制所有内容
    exportContainer.innerHTML = element.innerHTML

    // 处理地图截图
    const mapContainer = document.getElementById('amap-container')
    if (mapContainer && map) {
      const mapCanvas = mapContainer.querySelector('canvas')
      if (mapCanvas) {
        const mapSnapshot = mapCanvas.toDataURL('image/png')
        const exportMapContainer = exportContainer.querySelector('#amap-container')
        if (exportMapContainer) {
          exportMapContainer.innerHTML = `<img src="${mapSnapshot}" style="width:100%;height:100%;object-fit:cover;" />`
        }
      }
    }

    // 移除所有ant-card类,替换为纯div
    const cards = exportContainer.querySelectorAll('.ant-card')
    cards.forEach((card) => {
      const cardEl = card as HTMLElement
      try {
        cardEl.className = ''
        cardEl.style.setProperty('background-color', '#ffffff')
        cardEl.style.setProperty('border-radius', '12px')
        cardEl.style.setProperty('box-shadow', '0 4px 12px rgba(0, 0, 0, 0.1)')
        cardEl.style.setProperty('margin-bottom', '20px')
        cardEl.style.setProperty('overflow', 'hidden')
      } catch (err) {
        console.error('设置卡片样式失败:', err)
      }
    })

    // 处理卡片头部
    const cardHeads = exportContainer.querySelectorAll('.ant-card-head')
    cardHeads.forEach((head) => {
      const headEl = head as HTMLElement
      try {
        headEl.style.setProperty('background-color', '#667eea')
        headEl.style.setProperty('color', '#ffffff')
        headEl.style.setProperty('padding', '16px 24px')
        headEl.style.setProperty('font-size', '18px')
        headEl.style.setProperty('font-weight', '600')
      } catch (err) {
        console.error('设置卡片头部样式失败:', err)
      }
    })

    // 处理卡片内容
    const cardBodies = exportContainer.querySelectorAll('.ant-card-body')
    cardBodies.forEach((body) => {
      const bodyEl = body as HTMLElement
      bodyEl.style.setProperty('background-color', '#ffffff')
      bodyEl.style.setProperty('padding', '24px')
    })

    // 处理酒店卡片头部
    const hotelCards = exportContainer.querySelectorAll('.hotel-card')
    hotelCards.forEach((card) => {
      const head = card.querySelector('.ant-card-head') as HTMLElement
      if (head) {
        head.style.setProperty('background-color', '#1976d2')
      }
      (card as HTMLElement).style.setProperty('background-color', '#e3f2fd')
    })

    // 处理天气卡片
    const weatherCards = exportContainer.querySelectorAll('.weather-card')
    weatherCards.forEach((card) => {
      (card as HTMLElement).style.setProperty('background-color', '#e0f7fa')
    })

    // 处理预算总计
    const budgetTotal = exportContainer.querySelector('.budget-total')
    if (budgetTotal) {
      const el = budgetTotal as HTMLElement
      el.style.setProperty('background-color', '#667eea')
      el.style.setProperty('color', '#ffffff')
      el.style.setProperty('padding', '20px')
      el.style.setProperty('border-radius', '12px')
      el.style.setProperty('margin-bottom', '20px')
    }

    // 处理预算项
    const budgetItems = exportContainer.querySelectorAll('.budget-item')
    budgetItems.forEach((item) => {
      const el = item as HTMLElement
      el.style.setProperty('background-color', '#f5f7fa')
      el.style.setProperty('padding', '16px')
      el.style.setProperty('border-radius', '8px')
      el.style.setProperty('margin-bottom', '12px')
    })

    // 添加到body(隐藏)
    exportContainer.style.position = 'absolute'
    exportContainer.style.left = '-9999px'
    document.body.appendChild(exportContainer)

    const canvas = await html2canvas(exportContainer, {
      backgroundColor: '#f5f7fa',
      scale: 2,
      logging: false,
      useCORS: true,
      allowTaint: true
    })

    // 移除容器
    document.body.removeChild(exportContainer)

    const imgData = canvas.toDataURL('image/png')
    const pdf = new jsPDF({
      orientation: 'portrait',
      unit: 'mm',
      format: 'a4'
    })

    const imgWidth = 210 // A4宽度(mm)
    const imgHeight = (canvas.height * imgWidth) / canvas.width

    // 如果内容高度超过一页,分页处理
    let heightLeft = imgHeight
    let position = 0

    pdf.addImage(imgData, 'PNG', 0, position, imgWidth, imgHeight)
    heightLeft -= 297 // A4高度

    while (heightLeft > 0) {
      position = heightLeft - imgHeight
      pdf.addPage()
      pdf.addImage(imgData, 'PNG', 0, position, imgWidth, imgHeight)
      heightLeft -= 297
    }

    pdf.save(`旅行计划_${tripPlan.value?.city}_${new Date().getTime()}.pdf`)

    message.success({ content: 'PDF导出成功!', key: 'export' })
  } catch (error: any) {
    console.error('导出PDF失败:', error)
    message.error({ content: `导出PDF失败: ${error.message}`, key: 'export' })
  }
}

// 初始化地图
const initMap = async () => {
  try {
    const AMap = await AMapLoader.load({
      key: import.meta.env.VITE_AMAP_WEB_JS_KEY,  // 高德地图Web端(JS API) Key
      version: '2.0',
      plugins: ['AMap.Marker', 'AMap.Polyline', 'AMap.InfoWindow']
    })

    // 创建地图实例
    map = new AMap.Map('amap-container', {
      zoom: 12,
      center: [116.397128, 39.916527], // 默认中心点(北京)
      viewMode: '3D'
    })

    // 添加景点标记
    addAttractionMarkers(AMap)

    message.success('地图加载成功')
  } catch (error) {
    console.error('地图加载失败:', error)
    message.error('地图加载失败')
  }
}

// 添加景点标记
const addAttractionMarkers = (AMap: any) => {
  if (!tripPlan.value) return

  const markers: any[] = []
  const allAttractions: any[] = []

  // 收集所有景点
  tripPlan.value.days.forEach((day, dayIndex) => {
    day.attractions.forEach((attraction, attrIndex) => {
      if (attraction.location && attraction.location.longitude && attraction.location.latitude) {
        allAttractions.push({
          ...attraction,
          dayIndex,
          attrIndex
        })
      }
    })
  })

  // 创建标记
  allAttractions.forEach((attraction, index) => {
    const marker = new AMap.Marker({
      position: [attraction.location.longitude, attraction.location.latitude],
      title: attraction.name,
      label: {
        content: `<div style="background: #4CAF50; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px;">${index + 1}</div>`,
        offset: new AMap.Pixel(0, -30)
      }
    })

    // 创建信息窗口
    const infoWindow = new AMap.InfoWindow({
      content: `
        <div style="padding: 10px;">
          <h4 style="margin: 0 0 8px 0;">${escapeHtml(attraction.name)}</h4>
          <p style="margin: 4px 0;"><strong>地址:</strong> ${escapeHtml(attraction.address)}</p>
          <p style="margin: 4px 0;"><strong>游览时长:</strong> ${attraction.visit_duration}分钟</p>
          <p style="margin: 4px 0;"><strong>描述:</strong> ${escapeHtml(attraction.description)}</p>
          <p style="margin: 4px 0; color: #1890ff;"><strong>第${attraction.dayIndex + 1}天 景点${attraction.attrIndex + 1}</strong></p>
        </div>
      `,
      offset: new AMap.Pixel(0, -30)
    })

    // 点击标记显示信息窗口
    marker.on('click', () => {
      infoWindow.open(map, marker.getPosition())
    })

    markers.push(marker)
  })

  // 添加标记到地图
  map.add(markers)

  // 自动调整视野以包含所有标记
  if (allAttractions.length > 0) {
    map.setFitView(markers)
  }

  // 绘制路线
  drawRoutes(AMap, allAttractions)
}

// 绘制路线
const drawRoutes = (AMap: any, attractions: any[]) => {
  if (attractions.length < 2) return

  // 按天分组绘制路线
  const dayGroups: any = {}
  attractions.forEach(attr => {
    if (!dayGroups[attr.dayIndex]) {
      dayGroups[attr.dayIndex] = []
    }
    dayGroups[attr.dayIndex].push(attr)
  })

  // 为每天的景点绘制路线
  Object.values(dayGroups).forEach((dayAttractions: any) => {
    if (dayAttractions.length < 2) return

    const path = dayAttractions.map((attr: any) => [
      attr.location.longitude,
      attr.location.latitude
    ])

    const polyline = new AMap.Polyline({
      path: path,
      strokeColor: '#1890ff',
      strokeWeight: 4,
      strokeOpacity: 0.8,
      strokeStyle: 'solid',
      showDir: true // 显示方向箭头
    })

    map.add(polyline)
  })
}
</script>

<style scoped>
.result-container {
  min-height: 100vh;
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
  padding: 40px 20px;
}

.quality-alert {
  max-width: 1400px;
  margin: 0 auto 20px;
}

.warning-list {
  margin: 8px 0 0;
  padding-left: 20px;
}

.budget-note {
  margin: 12px 0 0;
  color: #666;
  font-size: 13px;
  line-height: 1.6;
}

.source-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ai-revise-button { background: #14213d; border-color: #14213d; }
.ai-revise-button:hover { background: #ff6b4a !important; border-color: #ff6b4a !important; }
.today-button { background: #21a179; border-color: #21a179; }
.today-button:hover { background: #17815f !important; border-color: #17815f !important; }

.history-intro { margin-bottom: 28px; padding: 16px; border: 1px solid #e3e9ef; border-radius: 14px; background: #f8fafc; }
.history-intro strong { color: #14213d; }
.history-intro p { margin: 5px 0 0; color: #7d899b; font-size: 12px; line-height: 1.6; }
.version-item { display: grid; grid-template-columns: 1fr auto; gap: 7px 12px; padding-bottom: 10px; }
.version-item > div { display: flex; gap: 9px; align-items: baseline; }
.version-item b { color: #14213d; }
.version-item span { color: #566276; font-size: 13px; }
.version-item small { grid-column: 1; color: #9aa4b3; }
.version-item .ant-btn, .version-item .ant-tag { grid-column: 2; grid-row: 1 / span 2; align-self: center; }

.revision-intro {
  display: flex;
  gap: 13px;
  padding: 16px;
  margin-bottom: 22px;
  border: 1px solid #e5eaf0;
  border-radius: 14px;
  background: #f8fafc;
}

.revision-agent-mark { width: 38px; height: 38px; flex: 0 0 auto; display: grid; place-items: center; border-radius: 11px; color: #ff8b73; background: #14213d; }
.revision-intro strong { color: #14213d; }
.revision-intro p { margin: 5px 0 0; color: #778398; font-size: 12px; line-height: 1.6; }
.quick-prompts { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 16px; }
.quick-prompts > span { width: 100%; margin-bottom: 2px; color: #68758a; font-size: 12px; font-weight: 700; }
.quick-prompts button { padding: 7px 10px; border: 1px solid #e0e6ed; border-radius: 8px; color: #5e6b7f; background: white; cursor: pointer; font-size: 12px; }
.quick-prompts button:hover { color: #d95437; border-color: #ffad9b; background: #fff6f3; }

.revision-progress { margin-top: 22px; padding: 15px; border-radius: 12px; background: #f7f9fc; }
.revision-progress-head { display: flex; justify-content: space-between; margin-bottom: 8px; color: #59677b; font-size: 12px; }
.revision-progress-head b { color: #e65334; }
.revision-events { display: flex; flex-direction: column; gap: 7px; margin-top: 12px; }
.revision-events span { display: flex; align-items: center; gap: 8px; color: #748095; font-size: 11px; }
.revision-events i { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 6px; color: white; background: #14213d; font-style: normal; }
.revision-history { margin-top: 24px; padding-top: 18px; border-top: 1px solid #e8ecf1; }
.revision-history > span { color: #778398; font-size: 11px; font-weight: 700; }
.revision-history p { margin: 8px 0 0; color: #3d7f65; font-size: 12px; }
.revision-footer { display: flex; align-items: center; justify-content: space-between; gap: 15px; }
.revision-footer > span { color: #8b96a7; font-size: 11px; }

@media (max-width: 600px) {
  .page-header { align-items: stretch; flex-direction: column; }
  .page-header > .ant-space { display: flex; flex-wrap: wrap; }
  .revision-footer { align-items: stretch; flex-direction: column; }
}

.page-header {
  max-width: 1200px;
  margin: 0 auto 30px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  animation: fadeInDown 0.6s ease-out;
}

.back-button {
  border-radius: 8px;
  font-weight: 500;
}

/* 内容布局 */
.content-wrapper {
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  gap: 24px;
}

.side-nav {
  width: 240px;
  flex-shrink: 0;
}

.side-nav :deep(.ant-menu) {
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  background: white;
}

.side-nav :deep(.ant-menu-item) {
  margin: 4px 8px;
  border-radius: 8px;
  transition: all 0.3s ease;
}

.side-nav :deep(.ant-menu-item-selected) {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.side-nav :deep(.ant-menu-item:hover) {
  background: rgba(102, 126, 234, 0.1);
}

.main-content {
  flex: 1;
  min-width: 0;
}

/* 景点图片样式 */
.attraction-image-wrapper {
  position: relative;
  margin-bottom: 12px;
  border-radius: 8px;
  overflow: hidden;
}

.attraction-image {
  width: 100%;
  height: 200px;
  object-fit: cover;
  transition: transform 0.3s ease;
}

.attraction-image-wrapper:hover .attraction-image {
  transform: scale(1.05);
}

.attraction-badge {
  position: absolute;
  top: 12px;
  left: 12px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

.badge-number {
  font-size: 18px;
}

.price-tag {
  position: absolute;
  top: 12px;
  right: 12px;
  background: rgba(255, 77, 79, 0.9);
  color: white;
  padding: 4px 12px;
  border-radius: 12px;
  font-weight: bold;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

.photo-source {
  position: absolute;
  right: 10px;
  bottom: 9px;
  padding: 4px 8px;
  border-radius: 7px;
  color: rgba(255,255,255,.92);
  background: rgba(15,24,40,.68);
  backdrop-filter: blur(6px);
  font-size: 9px;
  text-decoration: none;
}
.photo-source:hover { color: white; background: rgba(15,24,40,.85); }

/* 天气卡片样式 */
.weather-card {
  background: linear-gradient(135deg, #e0f7fa 0%, #b2ebf2 100%);
  border: none !important;
  transition: all 0.3s ease;
}

.weather-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 16px rgba(0, 0, 0, 0.15);
}

.weather-date {
  font-size: 16px;
  font-weight: bold;
  color: #00796b;
  margin-bottom: 12px;
  text-align: center;
}

.weather-info-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.weather-icon {
  font-size: 24px;
}

.weather-label {
  font-size: 12px;
  color: #666;
}

.weather-value {
  font-size: 16px;
  font-weight: 600;
  color: #00796b;
}

.weather-wind {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid rgba(0, 121, 107, 0.2);
  text-align: center;
  color: #00796b;
  font-size: 14px;
}

/* 回到顶部按钮 */
.back-top-button {
  width: 50px;
  height: 50px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: bold;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  cursor: pointer;
  transition: all 0.3s ease;
}

.back-top-button:hover {
  transform: scale(1.1);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.4);
}

/* 酒店卡片样式 */
.hotel-card {
  background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%);
  border: none !important;
}

.hotel-card :deep(.ant-card-head) {
  background: linear-gradient(135deg, #1976d2 0%, #1565c0 100%);
}

.hotel-title {
  color: white !important;
  font-weight: 600;
}

/* 顶部信息区布局 */
.top-info-section {
  display: flex;
  gap: 20px;
  margin-bottom: 20px;
}

.left-info {
  flex: 0 0 400px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.right-map {
  flex: 1;
}

/* 行程概览卡片 */
.overview-card {
  height: fit-content;
}

.overview-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.info-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.info-label {
  font-size: 14px;
  font-weight: 600;
  color: #666;
}

.info-value {
  font-size: 15px;
  color: #333;
  line-height: 1.6;
}

/* 预算卡片 */
.budget-card {
  height: fit-content;
}

.budget-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

.budget-item {
  text-align: center;
  padding: 12px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 8px;
  border: 1px solid #e8e8e8;
}

.budget-label {
  font-size: 13px;
  color: #666;
  margin-bottom: 8px;
}

.budget-value {
  font-size: 20px;
  font-weight: 700;
  color: #1890ff;
}

.budget-total {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 8px;
  color: white;
}

.total-label {
  font-size: 16px;
  font-weight: 600;
}

.total-value {
  font-size: 28px;
  font-weight: 700;
}

/* 地图卡片 */
.map-card {
  height: 100%;
  min-height: 500px;
}

.map-card :deep(.ant-card-body) {
  height: calc(100% - 57px);
  padding: 0;
}

/* 每日行程卡片 */
.days-card {
  margin-top: 20px;
}

.days-card-title { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.days-card-title :deep(.ant-btn) { color: #e65334; border-color: #ffc3b6; background: #fff7f4; }

.day-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.day-header > div { display: flex; align-items: center; gap: 10px; }
.day-header b { padding: 4px 8px; border-radius: 7px; color: #137254; background: #e8f7f1; font-size: 11px; }

.day-title {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

.day-date {
  font-size: 14px;
  color: #999;
}

.day-info {
  margin-bottom: 20px;
  padding: 16px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 8px;
  border: 1px solid #e8e8e8;
}

.daily-timeline { max-width: 920px; margin: 0 auto 28px; }
.intercity-card { margin-bottom: 18px; border: 1px solid #f0dfbd; background: #fffaf0; }
.return-card { border-color: #cfe6dc; background: #f4fbf8; }
.intercity-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.intercity-heading h3 { margin: 4px 0; color: #192235; font-size: 20px; }
.intercity-heading p { margin: 0; color: #647084; }
.intercity-kicker { color: #a56416; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.intercity-buffers { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 15px; }
.intercity-buffers span { padding: 7px 10px; color: #6e542c; background: #fff; border: 1px solid #efdfc5; border-radius: 8px; }
.timeline-slot { display: grid; grid-template-columns: 72px 38px 1fr; gap: 12px; min-height: 92px; position: relative; }
.timeline-slot:not(:last-child)::after { content: ''; position: absolute; left: 90px; top: 40px; bottom: -5px; width: 2px; background: #e4e9ef; }
.slot-time { display: flex; flex-direction: column; align-items: flex-end; padding-top: 5px; }
.slot-time b { color: #14213d; font-size: 14px; }
.slot-time span { color: #9aa4b2; font-size: 10px; }
.slot-marker { width: 34px; height: 34px; z-index: 1; display: grid; place-items: center; border-radius: 10px; color: white; background: #52637d; font-size: 11px; font-weight: 900; }
.slot-attraction .slot-marker { background: #ff6b4a; }
.slot-meal .slot-marker { background: #d59636; }
.slot-hotel .slot-marker { background: #4776e6; }
.slot-break .slot-marker { color: #24654f; background: #dff4eb; }
.slot-content { margin-bottom: 13px; padding: 13px 15px; border: 1px solid #e7ebf0; border-radius: 12px; background: #fbfcfd; }
.slot-content > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.slot-content h4 { margin: 0; color: #26344c; font-size: 15px; }
.slot-content p { margin: 6px 0 3px; color: #6f7b8e; font-size: 12px; line-height: 1.6; }
.slot-content small { color: #8d98a8; }
.slot-content em { display: block; margin-top: 6px; color: #a06b31; font-size: 10px; font-style: normal; }

.info-row {
  display: flex;
  gap: 12px;
  margin-bottom: 8px;
}

.info-row:last-child {
  margin-bottom: 0;
}

.info-row .label {
  font-weight: 600;
  color: #666;
  min-width: 100px;
}

.info-row .value {
  color: #333;
  flex: 1;
}

.route-anchor-note {
  display: block;
  margin-top: 6px;
  color: #778398;
  line-height: 1.6;
}

/* 卡片样式优化 */
:deep(.ant-card) {
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  margin-bottom: 20px;
  transition: all 0.3s ease;
  animation: fadeInUp 0.6s ease-out;
}

:deep(.ant-card:hover) {
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}

:deep(.ant-card-head) {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white !important;
  border-radius: 12px 12px 0 0;
  font-weight: 600;
}

:deep(.ant-card-head-title) {
  color: white !important;
  font-size: 18px;
}

:deep(.ant-card-head-title span) {
  color: white !important;
}

/* Collapse样式 */
:deep(.ant-collapse) {
  border: none;
  background: transparent;
}

:deep(.ant-collapse-item) {
  margin-bottom: 16px;
  border: 1px solid #e8e8e8;
  border-radius: 12px;
  overflow: hidden;
}

:deep(.ant-collapse-header) {
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  padding: 16px 20px !important;
  font-weight: 600;
}

:deep(.ant-collapse-content) {
  border-top: 1px solid #e8e8e8;
}

:deep(.ant-collapse-content-box) {
  padding: 20px;
}

/* 统计卡片样式 */
:deep(.ant-statistic-title) {
  font-size: 14px;
  color: #666;
  margin-bottom: 8px;
}

:deep(.ant-statistic-content) {
  font-size: 24px;
  font-weight: 600;
  color: #1890ff;
}

/* 景点卡片样式 */
:deep(.ant-list-item) {
  transition: all 0.3s ease;
}

:deep(.ant-list-item:hover) {
  transform: scale(1.02);
}

/* 动画 */
@keyframes fadeInDown {
  from {
    opacity: 0;
    transform: translateY(-20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* 响应式设计 */
@media (max-width: 768px) {
  .result-container {
    padding: 20px 10px;
  }

  .page-header {
    flex-direction: column;
    gap: 16px;
  }
}

/* TripMind 统一视觉主题 */
.result-container {
  background:
    radial-gradient(circle at 92% 4%, rgba(255, 107, 74, .08), transparent 24%),
    #f5f7fa;
  padding-top: 32px;
}

.page-header { max-width: 1400px; }

.back-button {
  color: #14213d;
  border-color: #dce3eb;
  background: white;
}

.back-button:hover { color: #e65334 !important; border-color: #ff9b85 !important; }

:deep(.ant-card) {
  border: 1px solid #e4eaf0;
  border-radius: 16px;
  box-shadow: 0 14px 36px rgba(33, 50, 77, .07);
}

:deep(.ant-card:hover) { box-shadow: 0 18px 44px rgba(33, 50, 77, .1); }

:deep(.ant-card-head) {
  color: #14213d !important;
  border-bottom: 1px solid #e7ebf0;
  border-radius: 16px 16px 0 0;
  background: white;
}

:deep(.ant-card-head-title),
:deep(.ant-card-head-title span) { color: #14213d !important; }

.hotel-card :deep(.ant-card-head) { background: #14213d; }
.hotel-card :deep(.ant-card-head-title),
.hotel-card :deep(.ant-card-head-title span) { color: white !important; }

.budget-total { background: #14213d; }
.budget-value { color: #e65334; }
.side-nav :deep(.ant-menu-item-selected) { color: white; background: #14213d; }
.side-nav :deep(.ant-menu-item:hover) { color: #e65334; background: #fff3ef; }
.back-top-button { background: #14213d; box-shadow: 0 8px 20px rgba(20, 33, 61, .25); }
.price-tag { background: rgba(230, 83, 52, .94); }
.attraction-badge { background: #14213d; }

@media (max-width: 900px) {
  .content-wrapper { display: block; }
  .side-nav { display: none; }
  .top-info-section { display: block; }
  .left-info { display: block; }
  .map-card { min-height: 390px; }
  .timeline-slot { grid-template-columns: 58px 32px 1fr; gap: 8px; }
  .timeline-slot:not(:last-child)::after { left: 73px; }
  .slot-marker { width: 30px; height: 30px; }
  .day-header { align-items: flex-start; flex-direction: column; gap: 5px; }
}
</style>
