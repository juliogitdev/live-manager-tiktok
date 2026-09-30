--[[
TikTok -> Roblox Live Manager v4
Coloque em ServerScriptService.

1) Game Settings > Security > Allow HTTP Requests = ON
2) Configure BASE_URL, API_KEY, TIKTOK_USERNAME e PROFILE.
3) Ligue as funções ActionHandlers às funções reais do seu jogo.

A API key deve ficar SOMENTE no servidor. Nunca use este código como LocalScript.
]]

local HttpService = game:GetService("HttpService")

local BASE_URL = "https://SEU-SERVICO.onrender.com"
local API_KEY = "SUA_API_KEY"
local TIKTOK_USERNAME = "sua_conta"
local PROFILE = "dance" -- raw | dance | kite

local LONG_POLL_SECONDS = 8
local RETRY_SECONDS = 2

local sessionId = nil
local cursor = 0

local processedIds = {}
local processedOrder = {}
local MAX_PROCESSED = 500

local function rememberEvent(id)
	if not id then return true end
	if processedIds[id] then return false end
	processedIds[id] = true
	table.insert(processedOrder, id)
	if #processedOrder > MAX_PROCESSED then
		local old = table.remove(processedOrder, 1)
		processedIds[old] = nil
	end
	return true
end

local function request(method, path, body)
	local options = {
		Url = BASE_URL .. path,
		Method = method,
		Headers = {
			["X-API-Key"] = API_KEY,
			["Content-Type"] = "application/json",
		},
	}
	if body ~= nil then
		options.Body = HttpService:JSONEncode(body)
	end

	local response = HttpService:RequestAsync(options)

	if not response.Success then
		return nil, response.StatusCode, response.Body
	end

	local ok, decoded = pcall(function()
		return HttpService:JSONDecode(response.Body)
	end)
	if not ok then
		return nil, 500, "Invalid JSON from backend"
	end

	return decoded, response.StatusCode, nil
end

local function register()
	local data, status, err = request("POST", "/api/bridge/register", {
		username = TIKTOK_USERNAME,
		profile = PROFILE,
		consumer_id = game.JobId ~= "" and game.JobId or "roblox-studio",
	})

	if not data then
		warn("Falha ao registrar bridge:", status, err)
		return false
	end

	sessionId = data.session_id
	cursor = data.cursor or 0
	table.clear(processedIds)
	table.clear(processedOrder)

	print("TikTok bridge registrado:", sessionId, "@" .. data.username, "perfil:", data.profile)
	return true
end

-- Conecte estas ações às funções reais do seu jogo.
local ActionHandlers = {}

ActionHandlers.heal = function(action, event)
	print("[ACTION heal]", action.amount, action.user and action.user.unique_id)
	-- Exemplo:
	-- GameController.Heal(action.user.unique_id, action.amount)
end

ActionHandlers.shield = function(action, event)
	print("[ACTION shield]", action.duration)
end

ActionHandlers.aura = function(action, event)
	print("[ACTION aura]", action.name, action.duration)
end

ActionHandlers.animation = function(action, event)
	print("[ACTION animation]", action.name)
end

ActionHandlers.giant = function(action, event)
	print("[ACTION giant]", action.scale, action.duration)
end

ActionHandlers.spawn = function(action, event)
	print("[ACTION spawn]", action.name, action.user and action.user.unique_id)
end

ActionHandlers.attack = function(action, event)
	print("[ACTION attack]", action.name)
end

ActionHandlers.sound = function(action, event)
	print("[ACTION sound]", action.name)
end

ActionHandlers.score = function(action, event)
	print("[ACTION score]", action.amount)
end

ActionHandlers.speed = function(action, event)
	print("[ACTION speed]", action.amount)
end

ActionHandlers.custom = function(action, event)
	print("[ACTION custom]", HttpService:JSONEncode(action))
end

local function handleEvent(event)
	if not rememberEvent(event.id) then
		return
	end

	if event.safety and event.safety.level == "CRITICAL" then
		warn("TikTok monitor: sinal CRÍTICO confirmado. Backend pode pausar ações.")
	elseif event.safety and event.safety.level == "ALERT" then
		warn("TikTok monitor: sinal em ALERTA; automação segue ativa até confirmação.")
	end

	for _, action in ipairs(event.actions or {}) do
		local handler = ActionHandlers[action.type]
		if handler then
			local ok, err = pcall(handler, action, event)
			if not ok then
				warn("Erro executando action", action.type, err)
			end
		else
			warn("Action desconhecida:", action.type)
		end
	end
end

while true do
	if not sessionId then
		if not register() then
			task.wait(RETRY_SECONDS)
			continue
		end
	end

	local path = string.format(
		"/api/bridge/%s/poll?after=%d&wait=%d&limit=100",
		sessionId,
		cursor,
		LONG_POLL_SECONDS
	)

	local data, status, err = request("GET", path)

	if not data then
		-- Render restartou ou a sessão deixou de existir: registra outra sessão automaticamente.
		if status == 404 or status == 410 then
			warn("Sessão do backend expirou. Registrando novamente...")
			sessionId = nil
			cursor = 0
		else
			warn("Erro no long poll:", status, err)
			task.wait(RETRY_SECONDS)
		end
		continue
	end

	if data.cursor_expired then
		-- Evita replay de presentes antigos quando o buffer já avançou.
		cursor = data.cursor or data.latest_seq or cursor
		warn("Cursor antigo expirou; avançando para o evento mais recente.")
		continue
	end

	for _, event in ipairs(data.events or {}) do
		handleEvent(event)
	end

	cursor = data.cursor or cursor

	if data.automation_paused then
		-- O backend continuará recebendo eventos, mas regras automáticas ficam sem ações.
		warn("Automações pausadas pelo monitor do backend.")
	end

	task.wait(0.15)
end
