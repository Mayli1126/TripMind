<template>
  <div class="result-container" :class="{ 'with-assistant': assistantOpen && savedTrip }">
    <aside v-if="savedTrip" v-show="assistantOpen" class="assistant-dock" aria-label="行程对话">
      <div class="assistant-dock-head"><div><strong>聊聊这份行程</strong><small>{{ selectedStop ? `正在查看：第${selectedStop.day.day_index + 1}天 · ${selectedStop.item.name}` : tripPlan?.city }}</small></div><button aria-label="收起行程对话" @click="closeTripAssistant">−</button></div>
      <TripAssistant ref="assistantPanel" embedded :trip-id="savedTrip.id" :initial-question="assistantQuestion" @updated="applyAssistantUpdate" />
    </aside>
    <!-- 结果页工具栏：常用操作直达，其余工具收进菜单 -->
    <nav class="trip-nav">
      <button class="nav-back" aria-label="返回首页" @click="goBack"><ResultIcon name="left" /></button>
      <div class="nav-tabs">
        <button
          v-for="tab in navTabs"
          :key="tab.id"
          class="nav-tab"
          :class="{ active: activeSection === tab.id }"
          @click="scrollToSection(tab.id)"
        >{{ tab.label }}</button>
      </div>
      <div class="nav-actions">
        <button v-if="!editMode" class="nav-ghost edit-nav" aria-label="编辑行程" :disabled="!tripPlan" @click="toggleEditMode"><ResultIcon name="edit" /><span>编辑行程</span></button>
        <template v-else>
          <button class="nav-ghost accent" @click="saveChanges"><ResultIcon name="check" />保存修改</button>
          <button class="nav-ghost" @click="cancelEdit">取消</button>
        </template>
        <button class="nav-ghost assistant-nav" :disabled="assistantUnavailable" @click="openTripAssistant()"><ResultIcon name="chat" /><span>{{ assistantOpening ? '正在保存…' : savedTrip ? '行程对话' : '保存并对话' }}</span></button>
        <a-dropdown :trigger="['click']">
          <template #overlay>
            <a-menu class="result-tools-menu">
              <a-menu-item key="revise" :disabled="!tripPlan" @click="openRevisionDrawer">用文字修改行程</a-menu-item>
              <a-menu-item v-if="savedTrip" key="refresh" :disabled="editMode || revisionLoading || !!pendingRevision || refreshingAdvisories" @click="refreshAdvisories">{{ refreshingAdvisories ? '正在核实…' : '核实开放与预约' }}</a-menu-item>
              <a-menu-item v-if="savedTrip" key="today" @click="router.push(`/trips/${savedTrip.id}/today`)">旅行计划表</a-menu-item>
              <a-menu-item v-if="savedTrip" key="history" @click="historyOpen = true">历史版本 · {{ savedTrip.versions.length }}</a-menu-item>
            </a-menu>
          </template>
          <button class="nav-ghost nav-more" aria-label="更多行程工具" title="更多行程工具"><ResultIcon name="more" /></button>
        </a-dropdown>
        <a-dropdown v-if="!editMode">
          <template #overlay>
            <a-menu>
              <a-menu-item key="image" @click="exportAsImage">导出为图片</a-menu-item>
              <a-menu-item key="pdf" @click="exportAsPDF">导出为 PDF</a-menu-item>
              <a-menu-divider />
              <a-menu-item key="share" @click="copyShareLink">复制分享链接</a-menu-item>
            </a-menu>
          </template>
          <button class="nav-primary" :disabled="!tripPlan"><ResultIcon name="download" /><span>导出行程</span><ResultIcon name="down" /></button>
        </a-dropdown>
      </div>
    </nav>

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
      <!-- 主内容区 -->
      <div class="main-content">
        <!-- 行程概览：封面轮播 + 日期区间 + 出发前贴士（页面首屏主体） -->
        <section id="overview" class="overview-section">
          <div class="overview-head">
            <div class="overview-title">
              <h1><span class="destination-name">{{ tripPlan.city }}</span><span class="trip-length">{{ tripPlan.days.length }}日行程</span></h1>
              <p class="overview-date">{{ tripPlan.start_date }} <i>至</i> {{ tripPlan.end_date }}<template v-if="totalAttractions"> · 共安排 {{ totalAttractions }} 个地点</template></p>
            </div>
            <div class="overview-stats">
              <div class="stat" v-if="tripPlan.budget">
                <span class="stat-label">预估总费用</span>
                <strong>¥{{ tripPlan.budget.total }}</strong>
                <small :class="{ 'over-budget': tripPlan.budget.over_budget }">
                  {{ tripPlan.budget.over_budget ? '超出预算' : `人均 ¥${tripPlan.budget.per_person}` }}
                </small>
              </div>
              <div class="stat" v-if="tripPlan.optimization_summary">
                <span class="stat-label">市内路程</span>
                <strong>{{ tripPlan.optimization_summary.total_distance_after_km }} km</strong>
                <small>已优化路线，少走 {{ tripPlan.optimization_summary.saved_percent }}% 弯路</small>
              </div>
              <div class="stat">
                <span class="stat-label">天气</span>
                <strong>{{ weatherBrief }}</strong>
                <small v-if="weatherRange">{{ weatherRange }}</small>
              </div>
            </div>
          </div>

          <CoverflowGallery
            v-if="coverflowItems.length"
            :items="coverflowItems"
            @select="onCoverSelect"
          />

        </section>


        <!-- 每日行程 -->
        <section id="days" class="days-block">

        <!-- 每日行程：按选中日期展示 -->
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

        <div class="itinerary-workspace">
        <div class="itinerary-list">
        <section
          v-for="day in visibleDays"
          :key="day.day_index"
          :id="`day-${day.day_index}`"
          class="day-section"
        >
          <div class="day-header">
            <div class="day-stepper" role="group" aria-label="切换行程日期">
              <button aria-label="前一天" :disabled="selectedDay <= 0" @click="selectDay(selectedDay - 1)"><ResultIcon name="left" /></button>
              <div class="day-stepper-copy" aria-live="polite"><strong>第{{ selectedDay + 1 }}天</strong><span class="day-date">{{ day.date }}</span></div>
              <button aria-label="后一天" :disabled="selectedDay >= tripPlan.days.length - 1" @click="selectDay(selectedDay + 1)"><ResultIcon name="right" /></button>
            </div>
          </div>
          <div class="route-tools"><h2 class="day-route-title" :title="day.theme || ''">当天路线</h2><button class="add-place" :disabled="assistantUnavailable" @click="openPlacePicker(day.day_index, day.attractions.length)">＋ 添加地点</button></div>
          <div v-if="weatherOfDay(day.date)" class="weather-strip" :class="{ [`wx-${weatherOfDay(day.date)!.tone}`]: true }">
            <span class="wx-now">{{ weatherOfDay(day.date)!.text }}</span>
            <span class="wx-temps">{{ weatherOfDay(day.date)!.day_temp }}°C / 夜间 {{ weatherOfDay(day.date)!.night_temp }}°C</span>
            <span class="wx-wind">{{ weatherOfDay(day.date)!.wind_direction }} {{ weatherOfDay(day.date)!.wind_power }}</span>
            <span v-for="tip in weatherOfDay(day.date)!.tips" :key="tip" class="wx-tip">{{ tip }}</span>
          </div>
          <div v-else class="weather-strip none"><span class="wx-now">当天暂无天气预报</span></div>

              <div :key="`${day.day_index}-${selectedDay}`" class="stop-list">
                <div v-for="(item, index) in day.attractions" :key="`${day.day_index}-${index}-${item.name}`" class="place-wrap"><button
                  class="stop-row" :class="{ selected: selectedStop?.item === item }"
                  :aria-pressed="selectedStop?.item === item" :data-attraction-name="item.name"
                  @click="selectStop(item)" @mouseenter="highlightMarker(item.name, true)" @mouseleave="highlightMarker(item.name, false)">
                  <span class="stop-number">{{ String(index + 1).padStart(2, '0') }}</span>
                  <span class="stop-thumbnail"><img :src="getAttractionImage(item.name, index)" :alt="item.name" loading="lazy" @error="handleImageError" /></span>
                  <span class="stop-name">{{ item.name }}<small>建议游玩 {{ item.visit_duration }} 分钟</small></span><ResultIcon class="stop-arrow" name="right" />
                </button>
                <button class="insert-place" :aria-label="`在${item.name}后添加地点`" :disabled="assistantUnavailable" @click="openPlacePicker(day.day_index, index + 1)">＋ 在此后插入地点</button></div>
                <p v-if="!day.attractions.length" class="stop-empty">这一天暂时没有景点安排。</p>
                <button class="append-place" :disabled="assistantUnavailable" @click="openPlacePicker(day.day_index, day.attractions.length)">＋ 继续添加当天地点</button>
                <button v-if="day.hotel" class="hotel-row" :class="{ selected: selectedHotel === day.hotel }"
                  :aria-pressed="selectedHotel === day.hotel" @click="selectHotel(day.hotel, day.day_index)"
                  @mouseenter="highlightMarker(`hotel:${day.day_index}`, true)" @mouseleave="highlightMarker(`hotel:${day.day_index}`, false)">
                  <span class="hotel-symbol" aria-hidden="true">住</span>
                  <span class="hotel-row-copy"><small>当天住宿</small><strong>{{ day.hotel.name || '酒店待选择' }}</strong>
                    <span>{{ day.hotel.address || day.hotel.type || day.accommodation || '具体位置待确认' }}</span></span>
                  <ResultIcon class="stop-arrow" name="right" />
                </button>
              </div>

        </section>
        </div>
        <aside class="itinerary-inspector">
            <section id="map" class="explorer-map">
              <div class="explorer-panel-heading"><h3>路线地图</h3><span class="explorer-live-dot">{{ selectedDay === -1 ? '全程路线' : `第${selectedDay + 1}天路线` }}</span></div>
              <div class="map-section">
                <div id="amap-container" style="width: 100%; height: 100%"></div>
                <div v-if="mapLoading" class="map-status">正在铺开你的旅行地图…</div>
                <div v-else-if="mapFailed" class="map-status"><strong>地图暂时未能加载</strong><span>景点与行程仍可正常查看</span><button @click="initMap">重新加载</button></div>
                <div v-if="selectedDay === -1 && tripPlan.days.length > 1" class="map-legend">
                  <span v-for="(_day, index) in tripPlan.days" :key="index" class="legend-item"><i :style="{ background: DAY_COLORS[index % DAY_COLORS.length] }"></i>第{{ index + 1 }}天</span>
                </div>
              </div>
            </section>
          <section class="stop-detail" aria-label="地点详细信息" aria-live="polite">
            <div v-if="selectedHotel" class="detail-content hotel-detail">
              <p class="hotel-detail-label">当天住宿参考</p>
              <StayReference :hotel="selectedHotel" compact />
              <p v-if="!validHotelLocation(selectedHotel)" class="hotel-location-note">酒店暂无有效坐标，暂未在地图标注；预订前请核实具体地址。</p>
            </div>
            <div v-else-if="selectedStop" :key="`${selectedStop.day.day_index}-${selectedStop.index}-${selectedStop.item.name}`" class="detail-content">
              <div class="detail-place-heading">
              <div class="detail-place-title">
                <p>第{{ selectedStop.day.day_index + 1 }}天 / 第{{ selectedStop.index + 1 }}站</p>
                <h2>{{ selectedStop.item.name }}</h2>
              </div>
              <div class="detail-photo attraction-image-wrapper" :data-attraction-name="selectedStop.item.name">
                <img :src="getAttractionImage(selectedStop.item.name, selectedStop.index)" :alt="selectedStop.item.name" @error="handleImageError" />
                <a v-if="attractionPhotoMeta[selectedStop.item.name]?.source" class="photo-source"
                  :href="attractionPhotoMeta[selectedStop.item.name].sourceUrl || undefined"
                  target="_blank" rel="noreferrer" :title="attractionPhotoMeta[selectedStop.item.name].attribution">图片 · {{ attractionPhotoMeta[selectedStop.item.name].source }}</a>
              </div>
              </div>
              <div v-if="editMode" class="detail-editor">
                <div class="detail-edit-actions">
                  <a-button size="small" :disabled="selectedStop.index === 0" @click="moveAttraction(selectedStop.day.day_index, selectedStop.index, 'up')">↑ 上移</a-button>
                  <a-button size="small" :disabled="selectedStop.index === selectedStop.day.attractions.length - 1" @click="moveAttraction(selectedStop.day.day_index, selectedStop.index, 'down')">↓ 下移</a-button>
                  <a-button size="small" danger @click="deleteAttraction(selectedStop.day.day_index, selectedStop.index)">删除景点</a-button>
                </div>
                <label>景点地址<a-input v-model:value="selectedStop.item.address" /></label>
                <label>游览时长（分钟）<a-input-number v-model:value="selectedStop.item.visit_duration" :min="10" :max="480" /></label>
                <label>景点介绍<a-textarea v-model:value="selectedStop.item.description" :rows="3" /></label>
              </div>
              <div v-else class="detail-copy">
                <div class="info-badges">
                  <span class="info-badge">建议 {{ selectedStop.item.visit_duration }} 分钟</span>
                  <span v-if="selectedStop.item.amap_average_cost != null" class="info-badge price">高德人均参考 ¥{{ selectedStop.item.amap_average_cost }}/人 · 非门票报价</span>
                  <span v-else-if="selectedStop.item.ticket_price_source === 'llm_estimate'" class="info-badge price" :title="selectedStop.item.ticket_price_note">AI门票估价 ¥{{ selectedStop.item.ticket_price }}/人 · 未核验</span>
                  <span v-else class="info-badge price" :title="selectedStop.item.ticket_price_note">{{ selectedStop.item.ticket_price == null || (selectedStop.item.ticket_price === 0 && !selectedStop.item.ticket_price_source) ? '门票待核实' : selectedStop.item.ticket_price === 0 ? '免费参考 · 以官方为准' : `门票参考 ¥${selectedStop.item.ticket_price}/人 · 非实时` }}</span>
                  <span v-if="selectedStop.item.rating" class="info-badge rating">★ {{ selectedStop.item.rating }}</span>
                </div>
                <p v-if="selectedStop.item.description" class="detail-description">{{ selectedStop.item.description }}</p>
                <div class="detail-facts">
                  <div v-if="selectedStop.item.opening_time"><span>开放参考</span><strong>{{ selectedStop.item.opening_time }}<template v-if="selectedStop.item.closing_time"> — {{ selectedStop.item.closing_time }}</template></strong></div>
                  <div v-if="selectedStop.item.address"><span>景点地址</span><strong>{{ selectedStop.item.address }}</strong></div>
                </div>
                <AttractionAdvisory :advisory="selectedStop.item.advisory" />
              </div>
            </div>
            <div v-else class="detail-empty"><span>⌖</span><h3>下一站，想去哪里？</h3><p>点击左侧景点，在这里查看详细信息。</p></div>
          </section>
        </aside>
        </div>
        </section>

        <section v-if="overallTips.length || tripPlan.data_sources?.length" class="trip-notes" aria-label="出行提示">
          <div v-if="overallTips.length" class="overview-tips">
            <h4>出发前须知</h4>
            <ol class="tips-list">
              <li v-for="(tip, index) in overallTips" :key="index"><span class="tip-no">{{ index + 1 }}</span><span class="tip-text">{{ tip }}</span></li>
            </ol>
          </div>
          <div v-if="tripPlan.data_sources?.length" class="overview-sources">
            <span class="sources-label">信息来自：</span>
            <a-tag v-for="source in tripPlan.data_sources" :key="`${source.category}-${source.name}`">{{ source.name }} · {{ getSourceTypeLabel(source.source_type) }}</a-tag>
          </div>
        </section>

        <!-- 费用明细：旅程安排看完后的核对区 -->
        <a-card v-if="tripPlan.budget" id="budget" class="budget-detail-card" :bordered="false">
          <template #title>费用与出行安排</template>
          <template v-if="tripPlan.budget">
          <div class="budget-grid">
            <div class="budget-item">
              <div class="budget-label">景点消费参考 · 优先高德人均</div>
              <div class="budget-value">{{ tripPlan.budget.unknown_ticket_count && !tripPlan.budget.total_attractions ? '待核实' : `¥${tripPlan.budget.total_attractions}` }}</div>
              <small v-if="tripPlan.budget.unknown_ticket_count">另有 {{ tripPlan.budget.unknown_ticket_count }} 处价格未知，未计入</small>
            </div>
            <div class="budget-item">
              <div class="budget-label">住宿消费参考 · 非酒店报价</div>
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
          </template>
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

    <PlacePicker :open="placePickerOpen" :city="tripPlan?.city || ''" :target="placeTarget" :existing="tripPlan?.days[placeDay]?.attractions || []" @close="placePickerOpen = false" @pick="addPickedPlace" />

    <a-drawer
      v-model:open="revisionOpen"
      title="AI 修改行程"
      placement="right"
      width="min(460px, 100vw)"
      :closable="!revisionLoading"
      class="revision-drawer"
    >
      <div class="revision-intro">
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
        <TaskDiagnostics :job-id="revisionJobId" />
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
import TaskDiagnostics from '@/components/TaskDiagnostics.vue'
import { computed, ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Alert as AAlert,
  BackTop as ABackTop,
  Button as AButton,
  Card as ACard,
  Drawer as ADrawer,
  Dropdown as ADropdown,
  Empty as AEmpty,
  Input as AInput,
  InputNumber as AInputNumber,
  Menu as AMenu,
  MenuItem as AMenuItem,
  MenuDivider as AMenuDivider,
  Modal as AModal,
  Progress as AProgress,
  Tag as ATag,
  Textarea as ATextarea,
  Timeline as ATimeline,
  TimelineItem as ATimelineItem,
  message
} from 'ant-design-vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import { API_BASE_URL, createSavedTrip, getSavedTrip, restoreTripVersion, reviseTripWithProgress, saveTripVersion, refreshTripAdvisories } from '@/services/api'
import type { Attraction, Hotel, PlanningProgressEvent, RevisionChange, SavedTripDetail, TripFormData, TripPlan } from '@/types'
import RevisionDiff from '@/components/RevisionDiff.vue'
import AttractionAdvisory from '@/components/AttractionAdvisory.vue'
import CoverflowGallery from '@/components/CoverflowGallery.vue'
import ResultIcon from '@/components/ResultIcon.vue'
import PlacePicker from '@/components/PlacePicker.vue'
import type { PlaceResult } from '@/components/PlacePicker.vue'
import TripAssistant from './TripAssistant.vue'
import StayReference from '@/components/StayReference.vue'
import type { CoverflowItem } from '@/components/CoverflowGallery.vue'

const router = useRouter()
const route = useRoute()
const tripPlan = ref<TripPlan | null>(null)
const editMode = ref(false)
const originalPlan = ref<TripPlan | null>(null)
const attractionPhotos = ref<Record<string, string>>({})
const attractionPhotoMeta = ref<Record<string, { source: string; sourceUrl?: string; attribution?: string }>>({})
const tripRequest = ref<TripFormData | null>(null)
const revisionOpen = ref(false)
const assistantOpen = ref(false)
const assistantQuestion = ref('')
const assistantPanel = ref<InstanceType<typeof TripAssistant>>()
const revisionInstruction = ref('')
const revisionLoading = ref(false)
const revisionJobId=ref('')
const revisionProgress = ref(0)
const revisionStatus = ref('')
const revisionEvents = ref<PlanningProgressEvent[]>([])
const revisionHistory = ref<string[]>([])
const pendingRevision = ref<{ plan: TripPlan; instruction: string; explanation: string; baseVersion?: number } | null>(null)
const diffOpen = ref(false)
const savedTrip = ref<SavedTripDetail | null>(null)
const refreshingAdvisories = ref(false)
async function refreshAdvisories() {
  if (!savedTrip.value || refreshingAdvisories.value) return
  refreshingAdvisories.value = true
  try {
    const result = await refreshTripAdvisories(savedTrip.value.id)
    savedTrip.value = result.trip; tripPlan.value = result.trip.plan
    message.info(result.partial ? `已处理 ${result.checked} 个地点，部分信息仍待核实，请查看卡片提示。` : `已更新 ${result.checked} 个地点的信息。`)
  } catch (error: any) { message.error(error.response?.data?.detail || '刷新失败，原计划仍保留') }
  finally { refreshingAdvisories.value = false }
}
const assistantOpening = ref(false)
const assistantUnavailable = computed(() => !tripPlan.value || editMode.value || revisionLoading.value || !!pendingRevision.value || assistantOpening.value || restoringVersion.value !== null)
const placePickerOpen = ref(false), placeDay = ref(0), placePosition = ref(0)
const placeTarget = computed(() => `第${placeDay.value + 1}天 · ${placePosition.value === 0 ? '当天开头' : `第${placePosition.value}站之后`}`)
function openPlacePicker(dayIndex: number, position: number) {
  if (assistantUnavailable.value) return
  if (assistantPanel.value?.hasPending()) { message.info('请先完成或放弃对话中的修改草稿'); return }
  placeDay.value = tripPlan.value!.days.findIndex(day => day.day_index === dayIndex)
  placePosition.value = position; placePickerOpen.value = true
}
async function addPickedPlace(place: PlaceResult) {
  if (assistantUnavailable.value || !tripPlan.value) return
  if (!tripRequest.value) { message.warning('当前计划缺少原始需求，暂时不能生成调整草稿'); return }
  if (assistantPanel.value?.hasPending()) { message.info('请先完成或放弃对话中的修改草稿'); return }
  placePickerOpen.value = false
  revisionInstruction.value = `仅调整第${placeDay.value + 1}天：在第${placePosition.value}个景点之后（0表示当天开头）添加「${place.name}」，POI ID ${place.id}，地址${place.address}，坐标${place.location.longitude},${place.location.latitude}。保留其余地点，核对时间、路线及预算；不可容纳时说明冲突，不擅自删除其他景点。`
  revisionOpen.value = true
  await submitRevision()
}
const historyOpen = ref(false)
const restoringVersion = ref<number | null>(null)
const quickRevisionPrompts = [
  '第二天安排轻松一点',
  '减少重复景点并优化路线',
  '优先保留免费景点',
  '把餐饮换成当地特色小吃'
]
let map: any = null
const mapLoading = ref(false)
const mapFailed = ref(false)
// 日期聚焦：-1 = 全程总览（多日不同颜色路线），>=0 聚焦某一天（只画当日路线）
const selectedDay = ref(0)
// 每天的专属颜色（地图路线、标记、日期标题共用，编号一一对应）
const DAY_COLORS = ['#ff6b4a', '#4776e6', '#21a179', '#9b59b6', '#d4a017', '#00a8a8', '#e67e22', '#7f8c8d']
const visibleDays = computed(() =>
  selectedDay.value === -1
    ? (tripPlan.value?.days || [])
    : (tripPlan.value && selectedDay.value < tripPlan.value.days.length
        ? [tripPlan.value.days[selectedDay.value]]
        : (tripPlan.value?.days || []))
)
const selectedAttraction = ref<Attraction | null>(null)
const hotelSelection = ref<Hotel | null>(null)
const selectedHotel = computed(() => visibleDays.value.find(day => day.hotel === hotelSelection.value)?.hotel || null)
const validMapLocation = (location: Attraction['location'] | Hotel['location']) => {
  return !!location && Number.isFinite(location.longitude) && Number.isFinite(location.latitude)
    && Math.abs(location.longitude) <= 180 && Math.abs(location.latitude) <= 90
    && (location.longitude !== 0 || location.latitude !== 0)
}
const validHotelLocation = (hotel: Hotel) => validMapLocation(hotel.location)
// A place click shows its neighborhood; day switching still fits the whole route.
const PLACE_FOCUS_ZOOM = 15
const selectHotel = (hotel: Hotel, dayIndex: number) => {
  if (selectedStop.value) highlightMarker(selectedStop.value.item.name, false)
  hotelSelection.value = hotel
  if (map && validHotelLocation(hotel)) {
    map.setZoomAndCenter(PLACE_FOCUS_ZOOM, [hotel.location!.longitude, hotel.location!.latitude])
    highlightMarker(`hotel:${dayIndex}`, true)
  }
  if (window.matchMedia('(max-width: 760px)').matches) {
    nextTick(() => document.querySelector('.stop-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
}
const visibleStops = computed(() => visibleDays.value.flatMap(day =>
  day.attractions.map((item, index) => ({ day, item, index }))
))
const selectedStop = computed(() =>
  selectedHotel.value ? null : visibleStops.value.find(stop => stop.item === selectedAttraction.value) || visibleStops.value[0] || null
)
const selectStop = (item: Attraction) => {
  visibleDays.value.forEach(day => highlightMarker(`hotel:${day.day_index}`, false))
  hotelSelection.value = null
  if (selectedStop.value) highlightMarker(selectedStop.value.item.name, false)
  selectedAttraction.value = item
  if (map && validMapLocation(item.location)) {
    map.setZoomAndCenter(PLACE_FOCUS_ZOOM, [item.location.longitude, item.location.latitude])
    highlightMarker(item.name, true)
  }
  nextTick(observePendingImages)
  if (window.matchMedia('(max-width: 760px)').matches) {
    nextTick(() => document.querySelector('.stop-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
}
// Reordering, deleting, switching days and restoring a version keep the inspector in sync.
watch(() => visibleStops.value.map(stop => `${stop.day.day_index}:${stop.item.name}:${stop.item.location?.longitude}:${stop.item.location?.latitude}`).join('|'), () => {
  if (map) nextTick(redrawRoutes)
})
watch(() => visibleDays.value.map(day => `${day.day_index}:${day.hotel?.name}:${day.hotel?.location?.longitude}:${day.hotel?.location?.latitude}`).join('|'), () => {
  if (map) nextTick(redrawRoutes)
})
// 行程被修改/恢复后天数可能变化：聚焦日越界时自动回到全程总览，避免渲染崩溃
watch(
  () => tripPlan.value?.days.length,
  (len) => {
    if (len !== undefined && selectedDay.value >= len) selectedDay.value = 0
  }
)
const totalAttractions = computed(
  () => (tripPlan.value?.days || []).reduce((sum, day) => sum + day.attractions.length, 0)
)
// 天气摘要（hero 条用）：首日天气 + 全程温度区间
const weatherBrief = computed(() => {
  const weather = tripPlan.value?.weather_info || []
  return weather.length ? `${weather[0].day_weather} ${weather[0].day_temp}°C` : '暂无预报'
})
const weatherRange = computed(() => {
  const weather = tripPlan.value?.weather_info || []
  if (!weather.length) return ''
  const highs = weather.map(w => w.day_temp || 0)
  return `全程 ${Math.min(...highs)}~${Math.max(...highs)}°C`
})
// 切换日期：重绘地图路线并滚动到行程区顶部
const selectDay = (index: number) => {
  if (!tripPlan.value || index < 0 || index >= tripPlan.value.days.length) return
  selectedDay.value = index
  hotelSelection.value = null
  selectedAttraction.value = null
  nextTick(() => {
    observePendingImages()
    document.querySelector('.day-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

// ---------------------------------------------------------------------------
// 顶部锚点导航：点击平滑滚动，滚动时自动高亮当前分区
// ---------------------------------------------------------------------------
// 有对应数据的分区才出现在导航里（无天气/无预算的行程不显示对应 Tab）
const navTabs = computed<{ id: string; label: string }[]>(() => {
  const tabs = [{ id: 'overview', label: '行程概览' }]
  if (tripPlan.value?.budget) tabs.push({ id: 'budget', label: '费用与安排' })
  tabs.push({ id: 'map', label: '景点地图' }, { id: 'days', label: '每日行程' })
  return tabs
})
const activeSection = ref('overview')

const scrollToSection = (id: string) => {
  activeSection.value = id
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// 滚动联动：取"最后一个顶边已滚过视口锚点线"的分区为当前 Tab
const syncActiveSection = () => {
  const anchorLine = window.scrollY + 200
  let currentId = 'overview'
  for (const tab of navTabs.value) {
    const el = document.getElementById(tab.id)
    if (el && el.getBoundingClientRect().top + window.scrollY <= anchorLine) currentId = tab.id
  }
  activeSection.value = currentId
}

// ---------------------------------------------------------------------------
// 行程概览：Coverflow 轮播数据 + 点击卡片跳到对应行程日
// ---------------------------------------------------------------------------
interface FlatAttraction { name: string; description: string; dayIndex: number }
const flatAttractions = computed<FlatAttraction[]>(() => {
  const result: FlatAttraction[] = []
  for (const [dayIndex, day] of (tripPlan.value?.days || []).entries()) {
    for (const attraction of day.attractions) {
      result.push({
        name: attraction.name,
        description: attraction.description || attraction.address || '',
        dayIndex
      })
    }
  }
  return result
})
const coverflowItems = computed<CoverflowItem[]>(() =>
  flatAttractions.value.map((item, index) => ({
    key: `${item.dayIndex}-${item.name}`,
    name: item.name,
    image: getAttractionImage(item.name, index),
    description: item.description
  }))
)
const onCoverSelect = (index: number) => {
  const target = flatAttractions.value[index]
  if (!target) return
  scrollToSection('days')
  selectDay(target.dayIndex)
  nextTick(() => focusAttractionCard(target.name))
}

// 出发前贴士：后端 overall_suggestions 为换行分隔的多条文本
const overallTips = computed(() =>
  (tripPlan.value?.overall_suggestions || '')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
)

// 复制行程分享链接：只有已保存的行程有稳定地址
const copyShareLink = async () => {
  if (!savedTrip.value) {
    message.warning('先保存行程，才能生成分享链接')
    return
  }
  const url = `${window.location.origin}/trips/${savedTrip.value.id}`
  try {
    await navigator.clipboard.writeText(url)
    message.success('分享链接已复制，可以发给同行伙伴')
  } catch {
    message.info(url)
  }
}
onMounted(async () => {
  const savedTripId = typeof route.params.id === 'string' ? route.params.id : ''
  if (savedTripId) {
    try {
      savedTrip.value = await getSavedTrip(savedTripId)
      tripPlan.value = savedTrip.value.plan
      tripRequest.value = savedTrip.value.request
      assistantOpen.value = true
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
  // 顶部 Tab 与滚动位置联动
  window.addEventListener('scroll', syncActiveSection, { passive: true })
})

const refreshPlanVisuals = async () => {
  await nextTick()
  // 地图优先加载，不再等待图片
  if (map) { map.destroy(); map = null }
  initMap()
  // 图片异步加载，不阻塞地图
  loadAttractionPhotos()
}

// 路由离开时销毁地图实例，防止高德 JSAPI 实例随反复进出页面而累积泄漏
onUnmounted(() => {
  window.removeEventListener('scroll', syncActiveSection)
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

const openTripAssistant = async (question = '') => {
  if (assistantUnavailable.value || !tripPlan.value) return
  assistantOpening.value = true
  try {
    if (!savedTrip.value) {
      if (!tripRequest.value) {
        message.warning('这份临时计划缺少原始需求，暂时不能保存并打开对话，请重新生成行程。')
        return
      }
      savedTrip.value = await createSavedTrip(tripRequest.value, tripPlan.value)
      tripPlan.value = savedTrip.value.plan
    }
    assistantQuestion.value = question
    assistantOpen.value = true
  } catch (error: any) {
    message.error(error.response?.data?.detail || '暂时无法打开行程对话，请稍后重试')
  } finally {
    assistantOpening.value = false
  }
}

const closeTripAssistant = () => {
  assistantOpen.value = false
}
const applyAssistantUpdate = async (trip: SavedTripDetail) => {
  savedTrip.value = trip
  tripPlan.value = trip.plan
  tripRequest.value = trip.request
  sessionStorage.setItem('tripPlan', JSON.stringify(trip.plan))
  sessionStorage.setItem('tripRequest', JSON.stringify(trip.request))
  await refreshPlanVisuals()
}

const openRevisionDrawer = () => {
  if (!tripRequest.value) {
    message.warning('当前行程缺少原始约束，请从首页重新生成后再使用 AI 修改')
    return
  }
  revisionOpen.value = true
}

const submitRevision = async () => {
  if (revisionLoading.value || !tripPlan.value || !tripRequest.value || revisionInstruction.value.trim().length < 2) return
  const instruction = revisionInstruction.value.trim()
  const baseVersion = savedTrip.value?.current_version
  revisionLoading.value = true
  revisionProgress.value = 0
  revisionStatus.value = '正在创建修改任务...'
  revisionEvents.value = []
  try {
    const revisedPlan = await reviseTripWithProgress({
      trip_request: tripRequest.value,
      plan: tripPlan.value,
      instruction,
      // 乐观锁：基于行程库当前版本修改，版本不一致时后端拒绝（防并发覆盖）
      base_version: baseVersion
    }, (event) => {
      revisionProgress.value = event.progress
      revisionStatus.value = event.message
      const previous = revisionEvents.value[revisionEvents.value.length - 1]
      if (!previous || previous.stage !== event.stage || previous.message !== event.message) {
        revisionEvents.value.push(event)
      }
      }, undefined, id=>{revisionJobId.value=id})
    // 阶段1批1：先展示差异确认，用户确认后才应用
    const diff = (revisedPlan.revision_diff || []) as RevisionChange[]
    pendingRevision.value = {
      baseVersion,
      plan: revisedPlan,
      instruction,
      explanation: diff.length ? `AI 理解的修改：${instruction}` : `修改范围较大：${instruction}`,
    }
    revisionOpen.value = false
    diffOpen.value = true
  } catch (error: any) {
      if(error?.name==='JobCancelled')message.info('修改已取消，当前计划未发生变化')
      else message.error(error.message || '修改失败，当前计划未发生变化')
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
      if (!pending.baseVersion) throw new Error('缺少修改基础版本，请重新生成修改建议')
      savedTrip.value = await saveTripVersion(savedTrip.value.id, revisedPlan, `AI 修改：${pending.instruction}`, pending.baseVersion)
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
  await assistantPanel.value?.refresh()
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



// 景点卡片只展示建议游玩时长，不展示容易被误认为承诺的精确到达/游玩时段。

const scheduleTimeLabel = (time: string, dayOffset = 0) => `${dayOffset === 1 ? '次日 ' : dayOffset > 1 ? `次日+${dayOffset - 1} ` : ''}${time}`
const journeyArrivalLabel = (journey: NonNullable<TripPlan['intercity_journey']>) => {
  const dayOffset = Math.round(
    (new Date(`${journey.arrival_date}T00:00:00`).getTime() - new Date(`${journey.departure_date}T00:00:00`).getTime()) / 86400000
  )
  return scheduleTimeLabel(journey.arrival_time, dayOffset)
}

// 滚动到指定区域

// 切换编辑模式
const toggleEditMode = () => {
  if (assistantPanel.value?.hasPending()) { message.info('请先完成或放弃对话中的修改草稿'); return }
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
        savedTrip.value = await saveTripVersion(savedTrip.value.id, tripPlan.value, '手动调整景点顺序或内容', savedTrip.value.current_version)
        tripPlan.value = savedTrip.value.plan
        await assistantPanel.value?.refresh()
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
    savedTrip.value = await restoreTripVersion(savedTrip.value.id, version, savedTrip.value.current_version)
    tripPlan.value = savedTrip.value.plan
    sessionStorage.setItem('tripPlan', JSON.stringify(tripPlan.value))
    historyOpen.value = false
    await refreshPlanVisuals()
    await assistantPanel.value?.refresh()
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
  // Pin the default first selection before reordering, so its detail stays selected.
  selectedAttraction.value = selectedStop.value?.item || null

  if (direction === 'up' && attrIndex > 0) {
    [attractions[attrIndex], attractions[attrIndex - 1]] = [attractions[attrIndex - 1], attractions[attrIndex]]
  } else if (direction === 'down' && attrIndex < attractions.length - 1) {
    [attractions[attrIndex], attractions[attrIndex + 1]] = [attractions[attrIndex + 1], attractions[attrIndex]]
  }
}

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

// 数据来源的通俗说法：用户只需要知道"这数据从哪来、可不可靠"
const getSourceTypeLabel = (type: string): string => ({
  live: '实时查询',
  estimated: '合理估算',
  generated: '智能生成',
  static: '基础资料'
}[type] || type)

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

  const apiBaseUrl = API_BASE_URL
  try {
    const res = await fetch(`${apiBaseUrl}/api/poi/photo?name=${encodeURIComponent(name)}&city=${encodeURIComponent(city)}`, { credentials: 'include' })
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

// 扫描 DOM 中尚未加载图片的景点容器并交给 IntersectionObserver 观察。
// 可重复调用：每日行程默认折叠、展开时才挂载 DOM，必须在展开后重新扫描
const observePendingImages = () => {
  const observer = imageObserver
  if (!observer) return
  document.querySelectorAll('[data-attraction-name]').forEach(el => {
    const name = el.getAttribute('data-attraction-name')
    if (name && !loadedImages.value.has(name)) {
      observer.observe(el)
    }
  })
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

  // 观察所有景点图片容器（首屏可见的；折叠面板展开后由 watch 补充观察）
  nextTick(() => {
    observePendingImages()
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
  if (mapLoading.value) return
  // The loader exposes reset at runtime, but its bundled declaration omits it.
  if (mapFailed.value) (AMapLoader as typeof AMapLoader & { reset(): void }).reset()
  mapLoading.value = true
  mapFailed.value = false
  try {
    const AMap = await AMapLoader.load({
      key: import.meta.env.VITE_AMAP_WEB_JS_KEY,  // 高德地图Web端(JS API) Key
      version: '2.0',
      plugins: ['AMap.Marker', 'AMap.Polyline', 'AMap.InfoWindow']
    })

    // 创建地图实例
    if (map) map.destroy()
    map = new AMap.Map('amap-container', {
      zoom: 12,
      center: [116.397128, 39.916527], // 默认中心点(北京)
      viewMode: '3D'
    })

    // 绘制行程路线（默认全程总览）
    drawRoutes(AMap)
  } catch (error) {
    mapFailed.value = true
    console.error('地图加载失败:', error)
    message.error('地图加载失败')
  } finally {
    mapLoading.value = false
  }
}

// 地图覆盖物（路线与标记）：切换日期时整组清除重绘
let mapOverlays: any[] = []
// 景点名 -> 地图标记索引：卡片 hover 高亮、点标记定位卡片用
let markerByName = new Map<string, any>()
const clearOverlays = () => {
  mapOverlays.forEach(overlay => overlay.setMap?.(null))
  mapOverlays = []
  markerByName = new Map()
  map?.clearMap?.()
}

// 按选中日期绘制路线：全程=每日一条彩色折线；单日=该日路线高亮 + 编号标记
const drawRoutes = (AMap: any) => {
  if (!map || !tripPlan.value) return
  clearOverlays()

  const days = selectedDay.value === -1
    ? tripPlan.value.days
    : [tripPlan.value.days[selectedDay.value]]

  const allPoints: [number, number][] = []

  days.forEach(day => {
    const color = DAY_COLORS[day.day_index % DAY_COLORS.length]
    const points: [number, number][] = []
    // Lodging is an independent marker, not an extra attraction or an invented route leg.
    if (day.hotel && validHotelLocation(day.hotel)) {
      const hotel = day.hotel
      const position: [number, number] = [hotel.location!.longitude, hotel.location!.latitude]
      const marker = new AMap.Marker({ position, title: hotel.name, anchor: 'center',
        content: '<div style="width:30px;height:30px;border-radius:9px;background:#167c80;color:white;display:grid;place-items:center;font-size:14px;font-weight:600;border:2px solid white;box-shadow:0 2px 6px #0003">住</div>' })
      marker.setMap(map)
      marker.on('click', () => selectHotel(hotel, day.day_index))
      markerByName.set(`hotel:${day.day_index}`, marker)
      mapOverlays.push(marker)
      allPoints.push(position)
    }
    // 当日动线仅连接景点；酒店独立标记，不虚构往返交通。
    day.attractions.forEach(attraction => {
      if (attraction.location?.longitude && attraction.location?.latitude) {
        points.push([attraction.location.longitude, attraction.location.latitude])
      }
    })
    if (points.length === 0) return

    // 路线折线
    const line = new AMap.Polyline({
      path: points,
      strokeColor: color,
      strokeWeight: selectedDay.value === -1 ? 4 : 6,
      strokeOpacity: 0.85,
      lineJoin: 'round',
      showDir: true,
    })
    line.setMap(map)
    mapOverlays.push(line)
    allPoints.push(...points)

    // 编号标记：与行程卡片编号一一对应（单日模式下为当日序号）
    let numberInDay = 0
    day.attractions.forEach((attraction, index) => {
      if (!attraction.location?.longitude || !attraction.location?.latitude) return
      numberInDay = index + 1
      const marker = new AMap.Marker({
        position: [attraction.location.longitude, attraction.location.latitude],
        title: attraction.name,
        anchor: 'center',
        content: `<div style="width:26px;height:26px;border-radius:50%;background:${color};color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;box-shadow:0 2px 6px rgba(0,0,0,.35);border:2px solid #fff;">${numberInDay}</div>`,
      })
      marker.setMap(map)
      mapOverlays.push(marker)
      markerByName.set(attraction.name, marker)
      // 点击标记：滚动定位到对应的行程卡片并高亮一下
      marker.on('click', () => selectStop(attraction))
    })
  })

  // 视野自适应：全程看整体，单日聚焦当日动线
  if (allPoints.length > 0) {
    map.setFitView(mapOverlays, false, [60, 60, 60, 60])
  }
  // SDK loading / route redraw can finish after a click. Preserve that explicit
  // selection rather than letting fitView pull the camera back to the whole day.
  const focusedHotel = selectedHotel.value
  const focusedPlace = visibleStops.value.find(stop => stop.item === selectedAttraction.value)?.item
  const focus = focusedHotel?.location || focusedPlace?.location
  if (validMapLocation(focus)) {
    map.setZoomAndCenter(PLACE_FOCUS_ZOOM, [focus!.longitude, focus!.latitude])
    highlightMarker(focusedHotel ? `hotel:${visibleDays.value.find(day => day.hotel === focusedHotel)!.day_index}` : focusedPlace!.name, true)
  }
}

// 当日天气条：按日期取预报并生成贴身提醒（雨具/防晒/保暖/防风）
const weatherOfDay = (dayDate: string) => {
  const weather = (tripPlan.value?.weather_info || []).find(w => w.date === dayDate)
  if (!weather) return null
  const text = `${weather.day_weather}转${weather.night_weather}`
  const icon = weatherIcon(weather.day_weather)
  const tips: string[] = []
  const all = `${weather.day_weather}${weather.night_weather}`
  if (all.includes('雨')) tips.push('☂ 记得带伞')
  if (all.includes('雪')) tips.push('❄ 路面可能结冰，穿防滑鞋')
  if ((weather.day_temp || 0) >= 35) tips.push('🥵 高温天：防晒补水，景点多安排室内')
  if ((weather.day_temp || 0) > 0 && (weather.day_temp || 0) >= 32 && (weather.day_temp || 0) < 35) tips.push('🌤 较热：备好水和防晒')
  if ((weather.day_temp || 0) <= 5) tips.push('🧣 低温天：注意保暖')
  // 风力取区间上限（如 "4-5级" 取 5），上限 ≥5 级才提醒，避免 "1-3级" 被误判
  const gust = Math.max(0, ...(weather.wind_power || '').match(/\d+/g)?.map(Number) || [0])
  if (gust >= 5 || (weather.wind_direction || '').includes('大风')) tips.push('💨 风较大：注意防风')
  // 提醒基调：有 tips 时偏警示色，否则中性
  const tone = tips.length ? 'alert' : 'mild'
  return { ...weather, text, icon, tips, tone }
}
const weatherIcon = (dayWeather: string): string => {
  if (dayWeather.includes('雷')) return '⛈️'
  if (dayWeather.includes('雨')) return '🌧️'
  if (dayWeather.includes('雪')) return '🌨️'
  if (dayWeather.includes('雾') || dayWeather.includes('霾')) return '🌫️'
  if (dayWeather.includes('阴')) return '☁️'
  if (dayWeather.includes('多云')) return '⛅'
  if (dayWeather.includes('晴')) return '☀️'
  return '🌤️'
}

// 点地图标记 -> 滚动到行程卡片并短暂高亮
const focusAttractionCard = (name: string) => {
  const stop = visibleStops.value.find(entry => entry.item.name === name)
  if (stop) selectStop(stop.item)
  // 卡片可能不在当前选中日：先切到所在日再定位
  const card = document.querySelector(`[data-attraction-name="${CSS.escape(name)}"]`)
  card?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  card?.classList.add('card-flash')
  setTimeout(() => card?.classList.remove('card-flash'), 1600)
}

// 卡片 hover -> 地图标记放大高亮（离开还原）
const highlightMarker = (name: string, on: boolean) => {
  const marker = markerByName.get(name)
  if (!marker) return
  const el = marker.getContent() as HTMLElement
  if (el?.style) {
    el.style.transform = on ? 'scale(1.45)' : 'scale(1)'
    el.style.zIndex = on ? '120' : ''
    el.style.transition = 'transform .15s ease'
  }
}

// 日期切换后重绘（AMap 实例已缓存）
const redrawRoutes = async () => {
  try {
    const AMap = await AMapLoader.load({
      key: import.meta.env.VITE_AMAP_WEB_JS_KEY,
      version: '2.0',
      plugins: ['AMap.Marker', 'AMap.Polyline']
    })
    drawRoutes(AMap)
  } catch (error) {
    console.error('路线绘制失败:', error)
  }
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

/* ===== P0 改版：地图主视区布局 ===== */
.hero-bar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.hero-main h1 { margin: 0; font-size: 28px; color: #14213d; letter-spacing: -.5px; }
.hero-main p { margin: 6px 0 0; color: #778398; font-size: 13px; }
.hero-stats { display: flex; gap: 28px; flex-wrap: wrap; }
.hero-stats .stat { display: flex; flex-direction: column; gap: 2px; }
.stat-label { font-size: 11px; color: #97a2b4; letter-spacing: 1px; }
.hero-stats .stat strong { font-size: 20px; color: #14213d; }
.hero-stats .stat small { font-size: 11px; color: #21a179; }
.hero-stats .stat small.over-budget { color: #e25822; font-weight: 700; }

.map-section {
  position: relative;
  height: 440px;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 8px 28px rgba(20, 33, 61, .1);
}
.map-legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 5;
  display: flex;
  gap: 12px;
  padding: 8px 12px;
  border-radius: 10px;
  background: rgba(255, 255, 255, .92);
  box-shadow: 0 2px 8px rgba(0, 0, 0, .12);
  font-size: 12px;
  color: #4a5a72;
}
.legend-item { display: inline-flex; align-items: center; gap: 5px; }
.legend-item i { width: 14px; height: 4px; border-radius: 2px; display: inline-block; }

/* 日期胶囊切换 */
.day-tabs {
  position: sticky;
  top: 72px;
  z-index: 20;
  display: flex;
  gap: 10px;
  padding: 12px 4px;
  margin: 4px -4px 20px;
  overflow-x: auto;
  background: rgba(248, 250, 252, .96);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid #e8edf3;
}
.day-tab {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  padding: 8px 16px;
  border: 1px solid #dfe6ee;
  border-radius: 999px;
  background: #fff;
  cursor: pointer;
  transition: all .18s ease;
}
.day-tab b { font-size: 13px; color: #14213d; }
.day-tab small { font-size: 11px; color: #97a2b4; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.day-tab:hover { border-color: #ff6b4a; transform: translateY(-1px); }
.day-tab.active { background: #14213d; border-color: #14213d; }
.day-tab.active b, .day-tab.active small { color: #fff; }

/* 单日行程小节 */
.day-section {
  margin-bottom: 28px;
  padding: 20px 22px;
  border: 1px solid #e8edf3;
  border-radius: 16px;
  background: #fff;
}
.day-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.day-title {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 999px;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  margin-right: 8px;
}
.day-date { color: #97a2b4; font-size: 12px; }
.budget-detail-card, .suggestion-card { margin-top: 20px; border-radius: 14px; }
.sources-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.sources-label { font-size: 12px; color: #97a2b4; }

/* 改版后主内容占满宽度 */
.content-wrapper { display: block; }
.main-content { max-width: 100%; }

/* 景点卡信息徽章 */
.attraction-info { display: flex; flex-direction: column; gap: 6px; }
.info-badges { display: flex; flex-wrap: wrap; gap: 6px; }
.info-badge {
  padding: 3px 10px;
  border-radius: 999px;
  background: #f2f5f9;
  color: #4a5a72;
  font-size: 12px;
  font-weight: 600;
}
.info-badge.price { background: #fff3ee; color: #d4502e; }
.info-badge.free { background: #eefaf4; color: #21a179; }
.info-badge.rating { background: #fff8e6; color: #b26a00; }
.info-desc { margin: 0; font-size: 12px; color: #667287; line-height: 1.6; }
.info-addr { margin: 0; font-size: 11px; color: #97a2b4; }

/* 地图标记点击后的卡片高亮 */
.card-flash { animation: cardFlash 1.6s ease; }
@keyframes cardFlash {
  0% { box-shadow: 0 0 0 0 rgba(255, 107, 74, .55); }
  30% { box-shadow: 0 0 0 6px rgba(255, 107, 74, .35); }
  100% { box-shadow: 0 0 0 0 rgba(255, 107, 74, 0); }
}

/* 当日天气条 */
.weather-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 18px;
  padding: 9px 14px;
  margin-bottom: 14px;
  border-radius: 10px;
  font-size: 12.5px;
  background: #f2f6fb;
  color: #4a5a72;
}
.weather-strip.alert { background: #fff7ef; }
.wx-now { font-size: 13px; font-weight: 700; color: #2c3a52; }
.wx-temps, .wx-wind { color: #667287; }
.wx-tip {
  padding: 3px 10px;
  border-radius: 999px;
  background: #ffedd9;
  color: #b25f0a;
  font-weight: 600;
}
.weather-strip.none { background: #f6f7fa; color: #97a2b4; }

/* ===== 深色沉浸改版：页面底色 + 顶部锚点导航 + 行程概览 ===== */
.result-container {
  background:
    radial-gradient(circle at 88% -4%, rgba(255, 107, 74, .14), transparent 26%),
    radial-gradient(circle at 4% 30%, rgba(71, 118, 230, .08), transparent 30%),
    #101b33;
  padding-top: 0;
}

/* 顶部导航：App 头部下方粘住，深色玻璃底 */
.trip-nav {
  position: sticky;
  top: 72px;
  z-index: 60;
  display: flex;
  align-items: center;
  gap: 16px;
  max-width: 1400px;
  margin: 0 auto;
  padding: 10px 20px;
  background: rgba(13, 23, 46, .86);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid rgba(255, 255, 255, .07);
}
.nav-back {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border: 1px solid rgba(255, 255, 255, .16);
  border-radius: 10px;
  color: #c7d2e4;
  background: transparent;
  font-size: 16px;
  cursor: pointer;
  transition: all .15s ease;
}
.nav-back:hover { color: #fff; border-color: #ff6b4a; }
.nav-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  scrollbar-width: none;
}
.nav-tabs::-webkit-scrollbar { display: none; }
.nav-tab {
  position: relative;
  flex: 0 0 auto;
  padding: 8px 14px 10px;
  border: 0;
  background: transparent;
  color: #8b99b3;
  font-size: 13.5px;
  font-weight: 700;
  cursor: pointer;
  transition: color .15s ease;
}
.nav-tab:hover { color: #d7e0ef; }
.nav-tab.active { color: #fff; }
.nav-tab.active::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 2px;
  height: 2px;
  border-radius: 2px;
  background: #ff6b4a;
}
.nav-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.nav-ghost {
  padding: 7px 12px;
  border: 1px solid rgba(255, 255, 255, .18);
  border-radius: 9px;
  color: #d7e0ef;
  background: transparent;
  font-size: 12.5px;
  font-weight: 700;
  white-space: nowrap;
  cursor: pointer;
  transition: all .15s ease;
}
.nav-ghost:hover { color: #fff; border-color: rgba(255, 255, 255, .4); }
.nav-ghost:disabled { opacity: .4; cursor: not-allowed; }
.nav-ghost.accent { border-color: #ff6b4a; color: #ff8b73; }
.nav-ghost.assistant-nav { border-color: #74d7e4; color: #9be9f2; background: rgba(69, 197, 215, .1); }
.assistant-entry { margin: 0 0 24px; padding: 20px 22px; border: 1px solid rgba(116, 215, 228, .25); border-radius: 18px; background: linear-gradient(115deg, rgba(65, 166, 191, .15), rgba(83, 119, 177, .08)); }
.assistant-entry-copy { display: flex; gap: 14px; align-items: flex-start; }
.assistant-entry-mark { color: #8be7f1; font-size: 28px; line-height: 1.4; }
.assistant-entry h3 { color: #ecfbff; margin: 0 0 6px; font-size: 17px; }
.assistant-entry p { color: #a9becd; margin: 0; font-size: 13px; line-height: 1.8; }
.assistant-quick-questions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.assistant-quick-questions button { padding: 9px 13px; border: 1px solid rgba(157, 213, 226, .22); border-radius: 10px; color: #c4e8ef; background: rgba(18, 37, 62, .25); font-size: 12px; cursor: pointer; }
.assistant-quick-questions button:hover { border-color: #74d7e4; }
.assistant-quick-questions .assistant-entry-action { color: #112e3a; background: #a0e6ef; font-weight: 700; }
.assistant-quick-questions button:disabled { opacity: .45; cursor: not-allowed; }
.assistant-entry > small { display: block; color: #dfbf91; margin-top: 10px; font-size: 12px; }
.nav-ghost i {
  font-style: normal;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 107, 74, .18);
  color: #ff8b73;
  font-size: 11px;
}
.nav-primary {
  padding: 8px 15px;
  border: 0;
  border-radius: 9px;
  color: #fff;
  background: #ff6b4a;
  font-size: 12.5px;
  font-weight: 800;
  white-space: nowrap;
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(255, 107, 74, .32);
  transition: all .15s ease;
}
.nav-primary:hover { background: #ff7d5e; transform: translateY(-1px); }
.nav-primary:disabled { opacity: .45; cursor: not-allowed; transform: none; }

/* 行程概览：深色沉浸首屏 */
.overview-section { padding: 34px 4px 8px; }
.overview-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
  margin-bottom: 26px;
}
.overview-title h1 { margin: 0; font-size: 30px; color: #fff; letter-spacing: -.5px; }
.overview-date { margin: 8px 0 0; color: #ff8b73; font-size: 13.5px; font-weight: 700; }
.overview-date i { margin: 0 4px; color: #7d8ba3; font-style: normal; font-weight: 500; }
.overview-stats { display: flex; gap: 30px; flex-wrap: wrap; }
.overview-stats .stat { display: flex; flex-direction: column; gap: 2px; }
.overview-stats .stat .stat-label { font-size: 11px; color: #8b99b3; letter-spacing: 1px; }
.overview-stats .stat strong { font-size: 20px; color: #fff; }
.overview-stats .stat small { font-size: 11px; color: #4fd39a; }
.overview-stats .stat small.over-budget { color: #ff8b73; font-weight: 700; }

/* 出发前贴士：橙色编号 + 两列排布 */
.overview-tips { margin-top: 30px; }
.overview-tips h4 {
  margin: 0 0 14px;
  color: #fff;
  font-size: 15px;
  letter-spacing: .5px;
}
.tips-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 26px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.tips-list li { display: flex; align-items: baseline; gap: 9px; }
.tip-no { flex: 0 0 auto; color: #ff6b4a; font-size: 13px; font-weight: 900; }
.tip-text { color: #aeb9ca; font-size: 12.5px; line-height: 1.7; }
.overview-sources {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 22px;
}
.overview-sources .sources-label { color: #7d8ba3; }

/* 深色底上的分区间距与白卡阴影 */
.map-block, .days-block { display: block; margin-top: 26px; }
.days-block .day-section,
.budget-detail-card,
#weather {
  box-shadow: 0 18px 40px rgba(3, 8, 20, .45);
}
.map-section { box-shadow: 0 18px 44px rgba(3, 8, 20, .5); }

/* 日期胶囊：让位顶部导航，改为深色玻璃 */
.day-tabs {
  top: 128px;
  background: rgba(16, 27, 51, .92);
  border-bottom: 1px solid rgba(255, 255, 255, .06);
}
.day-tab { background: #1a2648; border-color: #2c3a61; }
.day-tab b { color: #e8eefb; }
.day-tab small { color: #8b99b3; }
.day-tab.active { background: #ff6b4a; border-color: #ff6b4a; }
.day-tab.active b, .day-tab.active small { color: #fff; }

@media (max-width: 900px) {
  .trip-nav { flex-wrap: wrap; gap: 8px; padding: 8px 12px; }
  .nav-actions { margin-left: 0; }
  .nav-tab { padding: 6px 10px 8px; font-size: 12.5px; }
  .overview-title h1 { font-size: 24px; }
  .tips-list { grid-template-columns: 1fr; }
  .day-tabs { top: 116px; }
}
</style>
<style scoped src="../styles/result-journal.css"></style>
