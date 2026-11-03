<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div class="card budget-card">
        <div class="budget-header">
          <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
          <span class="budget-value">{{ formatMoney(budget) }}</span>
        </div>
        <input
          id="budget-slider"
          v-model.number="budget"
          type="range"
          min="0"
          :max="maxBudget"
          :step="BUDGET_STEP"
          class="budget-slider"
        />
        <div class="budget-scale">
          <span>{{ formatMoney(0) }}</span>
          <span>{{ formatMoney(maxBudget) }}</span>
        </div>
        <p class="budget-hint">{{ t('restocking.budgetHint') }}</p>
      </div>

      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.totalCost') }}</div>
          <div class="stat-value">{{ formatMoney(totalCost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ formatMoney(remainingBudget) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.itemsSelected') }}</div>
          <div class="stat-value">{{ recommendations.length }}</div>
        </div>
        <div class="stat-card warning">
          <div class="stat-label">{{ t('restocking.longestLeadTime') }}</div>
          <div class="stat-value">{{ t('restocking.days', { count: longestLeadTime }) }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendations') }}</h3>
          <button
            type="button"
            class="place-order-btn"
            :disabled="recommendations.length === 0 || submitting"
            @click="placeOrder"
          >
            {{ submitting ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="submitError" class="error submit-message">{{ submitError }}</div>
        <div v-if="placedOrder" class="success-message">
          <strong>{{ t('restocking.successTitle') }}</strong>
          <span>
            {{ t('restocking.successMessage', {
              orderNumber: placedOrder.order_number,
              date: formatDate(placedOrder.expected_delivery)
            }) }}
          </span>
          <router-link to="/orders">{{ t('restocking.viewOrders') }}</router-link>
        </div>

        <div v-if="recommendations.length === 0" class="empty-state">
          {{ t('restocking.noItems') }}
        </div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.itemName') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th class="num">{{ t('restocking.table.quantity') }}</th>
                <th class="num">{{ t('restocking.table.unitCost') }}</th>
                <th class="num">{{ t('restocking.table.lineTotal') }}</th>
                <th class="num">{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in recommendations" :key="item.item_sku">
                <td><strong>{{ item.item_sku }}</strong></td>
                <td>{{ item.item_name }}</td>
                <td>
                  <span :class="['badge', item.trend]">{{ t(`trends.${item.trend}`) }}</span>
                </td>
                <td class="num">{{ item.quantity }}</td>
                <td class="num">{{ formatMoney(item.unit_cost, 2) }}</td>
                <td class="num"><strong>{{ formatMoney(item.line_total) }}</strong></td>
                <td class="num">{{ t('restocking.days', { count: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency, formatCurrencyWithDecimals } from '../utils/currency'

// Slider granularity, in USD (the app's base currency; JPY is display-only)
const BUDGET_STEP = 1000

// Trend priority for ranking: lower number is restocked first
const TREND_RANK = { increasing: 0, stable: 1, decreasing: 2 }

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, currentLocale } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    const forecasts = ref([])
    const budget = ref(0)
    const submitting = ref(false)
    const submitError = ref(null)
    const placedOrder = ref(null)

    // Forecasts ranked by trend (increasing > stable > decreasing), then by
    // absolute growth (forecasted - current demand) descending.
    const rankedForecasts = computed(() => {
      return [...forecasts.value].sort((a, b) => {
        const trendDiff = (TREND_RANK[a.trend] ?? 3) - (TREND_RANK[b.trend] ?? 3)
        if (trendDiff !== 0) return trendDiff
        return (b.forecasted_demand - b.current_demand) - (a.forecasted_demand - a.current_demand)
      })
    })

    // Slider max = cost of restocking every forecast in full, rounded up to the
    // next step so that the max position always includes all items.
    const maxBudget = computed(() => {
      const total = forecasts.value.reduce((sum, f) => sum + f.forecasted_demand * f.unit_cost, 0)
      return Math.ceil(total / BUDGET_STEP) * BUDGET_STEP
    })

    // Greedy fill: walk the ranked list, taking each item's full forecasted
    // demand while it fits. The first item that does not fit gets a partial
    // quantity of floor(remaining / unit_cost) and filling stops there.
    // Items that end up with quantity 0 are dropped.
    const recommendations = computed(() => {
      const result = []
      let remaining = budget.value
      for (const f of rankedForecasts.value) {
        const fullCost = f.forecasted_demand * f.unit_cost
        let quantity
        let isLast = false
        if (fullCost <= remaining) {
          quantity = f.forecasted_demand
        } else {
          // Small epsilon guards against float error (e.g. 0.1 * 3) rounding down a unit
          quantity = Math.floor(remaining / f.unit_cost + 1e-9)
          isLast = true
        }
        if (quantity > 0) {
          const lineTotal = quantity * f.unit_cost
          result.push({
            item_sku: f.item_sku,
            item_name: f.item_name,
            trend: f.trend,
            quantity,
            unit_cost: f.unit_cost,
            line_total: lineTotal,
            lead_time_days: f.lead_time_days
          })
          remaining -= lineTotal
        }
        if (isLast) break
      }
      return result
    })

    const totalCost = computed(() => recommendations.value.reduce((sum, i) => sum + i.line_total, 0))
    // Clamp at 0 so float error never shows a tiny negative balance
    const remainingBudget = computed(() => Math.max(0, budget.value - totalCost.value))
    // Order lead time is the longest lead time among its items (matches backend)
    const longestLeadTime = computed(() =>
      recommendations.value.reduce((max, i) => Math.max(max, i.lead_time_days), 0)
    )

    const formatMoney = (amount, decimals = 0) => {
      return decimals > 0
        ? formatCurrencyWithDecimals(amount, currentCurrency.value, decimals)
        : formatCurrency(amount, currentCurrency.value)
    }

    const formatDate = (dateString) => {
      const date = new Date(dateString)
      if (isNaN(date.getTime())) return '-'
      return date.toLocaleDateString(currentLocale.value === 'ja' ? 'ja-JP' : 'en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
      })
    }

    const loadForecasts = async () => {
      try {
        loading.value = true
        error.value = null
        forecasts.value = await api.getDemandForecasts()
        // Start at 50% of max, snapped down to a slider step
        budget.value = Math.floor(maxBudget.value / 2 / BUDGET_STEP) * BUDGET_STEP
      } catch (err) {
        error.value = 'Failed to load demand forecasts: ' + err.message
        console.error('Load error:', err)
      } finally {
        loading.value = false
      }
    }

    const placeOrder = async () => {
      if (recommendations.value.length === 0 || submitting.value) return
      submitting.value = true
      submitError.value = null
      placedOrder.value = null
      try {
        // Only SKU and quantity are sent; the server recomputes prices itself
        placedOrder.value = await api.createRestockingOrder({
          items: recommendations.value.map(i => ({ item_sku: i.item_sku, quantity: i.quantity })),
          budget: budget.value
        })
      } catch (err) {
        const detail = err.response?.data?.detail
        submitError.value = t('restocking.error') + (typeof detail === 'string' ? ': ' + detail : '')
        console.error('Place order error:', err)
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadForecasts)

    return {
      t,
      BUDGET_STEP,
      loading,
      error,
      budget,
      maxBudget,
      recommendations,
      totalCost,
      remainingBudget,
      longestLeadTime,
      submitting,
      submitError,
      placedOrder,
      formatMoney,
      formatDate,
      placeOrder
    }
  }
}
</script>

<style scoped>
.budget-card {
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}

.budget-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.budget-value {
  font-size: 1.75rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  width: 100%;
  accent-color: #0f172a;
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  font-size: 0.813rem;
  color: #64748b;
  margin-top: 0.25rem;
}

.budget-hint {
  margin: 0.75rem 0 0;
  font-size: 0.813rem;
  color: #64748b;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.success-message {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin: 1rem 1.5rem;
  padding: 0.75rem 1rem;
  background: #d1fae5;
  border: 1px solid #10b981;
  border-radius: 6px;
  color: #065f46;
  font-size: 0.875rem;
}

.success-message a {
  color: #065f46;
  font-weight: 600;
  text-decoration: underline;
}

.submit-message {
  margin: 1rem 1.5rem;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
  font-size: 0.875rem;
}

th.num,
td.num {
  text-align: right;
}
</style>
